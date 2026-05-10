"""Launch Research Assistant as a native desktop-style app window.

The app still runs as a local FastAPI server, while pywebview provides a real
desktop shell without browser tabs or an address bar. If pywebview is not
available on a machine, the launcher falls back to Chromium app mode.
"""
from __future__ import annotations

import argparse
import os
import socket
import subprocess
import threading
import time
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parent
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8050
DEFAULT_WIDTH = int(os.environ.get("RESEARCH_ASSISTANT_WIDTH", "1440"))
DEFAULT_HEIGHT = int(os.environ.get("RESEARCH_ASSISTANT_HEIGHT", "920"))


class DesktopWindowApi:
    def __init__(self) -> None:
        self._window = None
        self._maximized = False
        self._drag_origin: tuple[int, int] | None = None

    def minimize(self) -> None:
        if self._window:
            self._window.minimize()

    def toggle_maximize(self) -> None:
        if not self._window:
            return
        if self._maximized:
            self._window.restore()
        else:
            self._window.maximize()
        self._maximized = not self._maximized

    def close(self) -> None:
        if self._window:
            self._window.destroy()

    def resize(self, width: int, height: int) -> None:
        if self._window:
            self._window.resize(max(980, int(width)), max(680, int(height)))

    def begin_drag(self) -> None:
        if self._window:
            self._drag_origin = (int(self._window.x), int(self._window.y))

    def drag_to(self, dx: int, dy: int) -> None:
        if self._window and self._drag_origin:
            x, y = self._drag_origin
            self._window.move(x + int(dx), y + int(dy))

    def end_drag(self) -> None:
        self._drag_origin = None


def _find_free_port(host: str, start: int) -> int:
    for port in range(start, start + 40):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.25)
            if sock.connect_ex((host, port)) != 0:
                return port
    raise RuntimeError(f"No free port found from {start} to {start + 39}")


def _wait_until_ready(url: str, timeout: float = 20.0) -> None:
    deadline = time.time() + timeout
    health_url = f"{url}/health"
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(health_url, timeout=1.5) as resp:
                if resp.status == 200:
                    return
        except Exception:
            time.sleep(0.25)
    raise RuntimeError(f"Server did not become ready: {health_url}")


def _browser_candidates() -> list[Path]:
    env_browser = os.environ.get("RESEARCH_ASSISTANT_BROWSER", "").strip()
    candidates = [Path(env_browser)] if env_browser else []
    candidates.extend([
        Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
        Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
        Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
        Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
    ])
    return [p for p in candidates if p and p.exists()]


def _launch_app_window(url: str, width: int, height: int) -> subprocess.Popen | None:
    browsers = _browser_candidates()
    if not browsers:
        return None

    profile_dir = ROOT / "data" / "desktop-profile"
    profile_dir.mkdir(parents=True, exist_ok=True)
    args = [
        str(browsers[0]),
        f"--app={url}/?desktop=1",
        f"--window-size={width},{height}",
        "--new-window",
        "--no-first-run",
        "--disable-features=Translate",
        f"--user-data-dir={profile_dir}",
    ]
    return subprocess.Popen(args)


def _launch_webview_window(url: str, width: int, height: int) -> bool:
    try:
        import webview
    except ImportError:
        return False

    try:
        api = DesktopWindowApi()
        window = webview.create_window(
            "Research Assistant",
            f"{url}/?desktop=1",
            js_api=api,
            width=width,
            height=height,
            min_size=(980, 680),
            resizable=True,
            text_select=True,
            confirm_close=True,
            background_color="#FFFFFF",
            frameless=False,
            easy_drag=True,
            draggable=False,
        )
        api._window = window
        webview.start(debug=False, private_mode=False)
        return bool(window)
    except Exception as exc:
        print(f"pywebview failed to start, falling back to browser shell: {exc}")
        return False


def _run_server(host: str, port: int):
    import uvicorn

    config = uvicorn.Config(
        "app.api.server:app",
        host=host,
        port=port,
        log_level="warning",
        reload=False,
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    return server, thread


def main() -> int:
    parser = argparse.ArgumentParser(description="Research Assistant desktop app")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--width", type=int, default=DEFAULT_WIDTH)
    parser.add_argument("--height", type=int, default=DEFAULT_HEIGHT)
    parser.add_argument("--browser-shell", action="store_true",
                        help="Use the old Edge/Chrome app-mode shell instead of pywebview")
    args = parser.parse_args()

    os.chdir(ROOT)
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass

    port = _find_free_port(args.host, args.port)
    url = f"http://{args.host}:{port}"
    server, thread = _run_server(args.host, port)
    try:
        _wait_until_ready(url)
        if not args.browser_shell and _launch_webview_window(url, args.width, args.height):
            print(f"Research Assistant native window: {url}")
            return 0

        proc = _launch_app_window(url, args.width, args.height)
        if proc is None:
            import webbrowser

            print("No Edge/Chrome executable found; falling back to default browser.")
            webbrowser.open(url)
            while thread.is_alive():
                time.sleep(0.5)
            return 0

        print(f"Research Assistant desktop window: {url}")
        proc.wait()
        return 0
    except KeyboardInterrupt:
        return 130
    finally:
        server.should_exit = True
        thread.join(timeout=5)


if __name__ == "__main__":
    raise SystemExit(main())
