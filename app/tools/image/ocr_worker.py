import json
import sys

from app.tools.image.ocr_engine import PaddleOCREngine

_PREFIX = "__OCR_JSON__"


def main() -> int:
    if len(sys.argv) >= 2 and sys.argv[1] == "--server":
        lang = sys.argv[2] if len(sys.argv) > 2 else "ch"
        return run_server(lang)

    if len(sys.argv) < 2:
        print(json.dumps({"success": False, "error": "image_path is required"}, ensure_ascii=False))
        return 2

    image_path = sys.argv[1]
    lang = sys.argv[2] if len(sys.argv) > 2 else "ch"
    try:
        result = PaddleOCREngine(lang=lang).extract_text(image_path)
    except Exception as exc:
        print(json.dumps({"success": False, "error": str(exc)}, ensure_ascii=False))
        return 1

    print(
        json.dumps(
            {
                "success": True,
                "ocr_result": result.model_dump(),
            },
            ensure_ascii=False,
        )
    )
    return 0


def run_server(lang: str) -> int:
    try:
        engine = PaddleOCREngine(lang=lang)
        engine._client()
        _send({"type": "ready", "success": True})
    except Exception as exc:
        _send({"type": "ready", "success": False, "error": str(exc)})
        return 1

    for raw_line in sys.stdin:
        try:
            request = json.loads(raw_line)
            request_id = str(request.get("id") or "")
            image_path = str(request.get("image_path") or "")
            if not request_id or not image_path:
                _send({"id": request_id, "success": False, "error": "id and image_path are required"})
                continue
            result = engine.extract_text(image_path)
            _send({"id": request_id, "success": True, "ocr_result": result.model_dump()})
        except Exception as exc:
            _send({"id": str(locals().get("request_id", "")), "success": False, "error": str(exc)})
    return 0


def _send(payload: dict) -> None:
    print(_PREFIX + json.dumps(payload, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
