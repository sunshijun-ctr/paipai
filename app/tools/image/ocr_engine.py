import json
import os
import queue
import subprocess
import sys
import threading
import time
import uuid

from app.tools.image.schemas import OCRResult

_WORKER_PREFIX = "__OCR_JSON__"


class PaddleOCREngine:
    def __init__(self, lang: str = "ch") -> None:
        self.lang = lang
        self._ocr = None

    def _client(self):
        if self._ocr is None:
            try:
                from paddleocr import PaddleOCR
            except ImportError as exc:
                raise RuntimeError(
                    "PaddleOCR is not installed. Install paddleocr and paddlepaddle to enable OCR."
                ) from exc
            # Newer PaddleOCR builds expose `predict()` and can fail in this
            # environment when MKLDNN and doc pre-processors are enabled.
            self._ocr = PaddleOCR(
                lang=self.lang,
                use_doc_orientation_classify=False,
                use_doc_unwarping=False,
                use_textline_orientation=False,
                enable_mkldnn=False,
            )
        return self._ocr

    def extract_text(self, image_path: str) -> OCRResult:
        client = self._client()
        try:
            result = client.predict(image_path) if hasattr(client, "predict") else client.ocr(image_path)
        except TypeError:
            result = client.ocr(image_path)
        texts: list[str] = []
        confidences: list[float] = []

        if result:
            if self._looks_like_predict_output(result):
                texts, confidences = self._collect_predict_output(result)
            else:
                texts, confidences = self._collect_legacy_output(result)

        confidence = sum(confidences) / len(confidences) if confidences else None
        return OCRResult(text="\n".join(texts).strip(), confidence=confidence)

    def extract_text_isolated(self, image_path: str, timeout: int = 30) -> OCRResult:
        env = os.environ.copy()
        env.setdefault("PYTHONIOENCODING", "utf-8")
        try:
            proc = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "app.tools.image.ocr_worker",
                    image_path,
                    self.lang,
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                env=env,
            )
        except subprocess.TimeoutExpired as exc:
            detail = (exc.stderr or exc.stdout or "").strip()
            raise RuntimeError(
                f"OCR worker timed out after {timeout}s while initializing/running PaddleOCR. {detail[-400:]}"
            ) from exc
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip()
            raise RuntimeError(detail[-600:] or f"OCR worker exited with code {proc.returncode}")

        for line in reversed((proc.stdout or "").splitlines()):
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not payload.get("success"):
                raise RuntimeError(str(payload.get("error") or "OCR worker failed"))
            return OCRResult(**(payload.get("ocr_result") or {}))
        raise RuntimeError("OCR worker did not return a valid JSON result")

    def preheat(self) -> None:
        persistent_ocr_worker(self.lang).start()

    def extract_text_persistent(self, image_path: str, timeout: int = 30) -> OCRResult:
        return persistent_ocr_worker(self.lang).extract_text(image_path, timeout=timeout)

    @staticmethod
    def _looks_like_predict_output(result) -> bool:
        return bool(
            isinstance(result, list)
            and result
            and isinstance(result[0], dict)
            and ("rec_texts" in result[0] or "rec_scores" in result[0])
        )

    @staticmethod
    def _collect_predict_output(result) -> tuple[list[str], list[float]]:
        texts: list[str] = []
        confidences: list[float] = []
        for page in result:
            if not isinstance(page, dict):
                continue
            page_texts = page.get("rec_texts") or []
            page_scores = page.get("rec_scores") or []
            for idx, raw_text in enumerate(page_texts):
                text = str(raw_text).strip()
                if not text:
                    continue
                texts.append(text)
                if idx < len(page_scores):
                    try:
                        confidences.append(float(page_scores[idx]))
                    except (TypeError, ValueError):
                        pass
        return texts, confidences

    @staticmethod
    def _collect_legacy_output(result) -> tuple[list[str], list[float]]:
        texts: list[str] = []
        confidences: list[float] = []
        for page in result:
            if not page:
                continue
            for line in page:
                try:
                    text = str(line[1][0]).strip()
                    score = float(line[1][1])
                except (IndexError, TypeError, ValueError):
                    continue
                if text:
                    texts.append(text)
                    confidences.append(score)
        return texts, confidences


class PersistentOCRWorker:
    def __init__(self, lang: str = "ch") -> None:
        self.lang = lang
        self._proc: subprocess.Popen | None = None
        self._responses: queue.Queue[dict] = queue.Queue()
        self._lock = threading.RLock()
        self._reader: threading.Thread | None = None
        self._starting = False

    def start(self) -> None:
        with self._lock:
            if self._proc and self._proc.poll() is None:
                return
            if self._starting:
                return
            self._starting = True
            thread = threading.Thread(target=self._start_blocking, daemon=True)
            thread.start()

    def extract_text(self, image_path: str, timeout: int = 30) -> OCRResult:
        self._ensure_started(timeout=min(timeout, 90))
        proc = self._proc
        if not proc or proc.poll() is not None or proc.stdin is None:
            raise RuntimeError("OCR worker is not running")

        request_id = uuid.uuid4().hex
        payload = json.dumps({"id": request_id, "image_path": image_path}, ensure_ascii=False)
        try:
            with self._lock:
                proc.stdin.write(payload + "\n")
                proc.stdin.flush()
        except Exception as exc:
            self.restart()
            raise RuntimeError(f"failed to send OCR request: {exc}") from exc

        deadline = time.monotonic() + timeout
        deferred: list[dict] = []
        try:
            while time.monotonic() < deadline:
                remaining = max(deadline - time.monotonic(), 0.1)
                try:
                    message = self._responses.get(timeout=min(0.5, remaining))
                except queue.Empty:
                    if proc.poll() is not None:
                        raise RuntimeError(f"OCR worker exited with code {proc.returncode}")
                    continue
                if message.get("id") != request_id:
                    deferred.append(message)
                    continue
                for item in deferred:
                    self._responses.put(item)
                if not message.get("success"):
                    raise RuntimeError(str(message.get("error") or "OCR worker failed"))
                return OCRResult(**(message.get("ocr_result") or {}))
        finally:
            for item in deferred:
                self._responses.put(item)

        self.restart()
        raise RuntimeError(f"OCR worker timed out after {timeout}s")

    def restart(self) -> None:
        with self._lock:
            proc = self._proc
            self._proc = None
            if proc and proc.poll() is None:
                try:
                    proc.kill()
                except Exception:
                    pass
            self._starting = False
        self.start()

    def _ensure_started(self, timeout: int) -> None:
        self.start()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            proc = self._proc
            if proc and proc.poll() is None and not self._starting:
                return
            time.sleep(0.1)
        raise RuntimeError(f"OCR worker did not become ready within {timeout}s")

    def _start_blocking(self) -> None:
        env = os.environ.copy()
        env.setdefault("PYTHONIOENCODING", "utf-8")
        proc = subprocess.Popen(
            [
                sys.executable,
                "-u",
                "-m",
                "app.tools.image.ocr_worker",
                "--server",
                self.lang,
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
        )
        with self._lock:
            self._proc = proc
            self._reader = threading.Thread(target=self._read_stdout, args=(proc,), daemon=True)
            self._reader.start()

        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                with self._lock:
                    self._starting = False
                return
            try:
                message = self._responses.get(timeout=0.5)
            except queue.Empty:
                continue
            if message.get("type") != "ready":
                self._responses.put(message)
                continue
            with self._lock:
                self._starting = False
            if not message.get("success"):
                self.restart()
            return

        with self._lock:
            self._starting = False
            if self._proc is proc:
                self._proc = None
            if proc.poll() is None:
                try:
                    proc.kill()
                except Exception:
                    pass

    def _read_stdout(self, proc: subprocess.Popen) -> None:
        if proc.stdout is None:
            return
        for line in proc.stdout:
            line = line.strip()
            if not line.startswith(_WORKER_PREFIX):
                continue
            try:
                self._responses.put(json.loads(line[len(_WORKER_PREFIX):]))
            except json.JSONDecodeError:
                continue


_PERSISTENT_WORKERS: dict[str, PersistentOCRWorker] = {}
_PERSISTENT_WORKERS_LOCK = threading.Lock()


def persistent_ocr_worker(lang: str = "ch") -> PersistentOCRWorker:
    with _PERSISTENT_WORKERS_LOCK:
        worker = _PERSISTENT_WORKERS.get(lang)
        if worker is None:
            worker = PersistentOCRWorker(lang=lang)
            _PERSISTENT_WORKERS[lang] = worker
        return worker
