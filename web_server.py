"""Start the Research Assistant web server and open the browser.

Usage:
    python web_server.py            # default: localhost:8000
    python web_server.py --port 9000
"""
import argparse
import asyncio
import logging
import os
import sys
import threading
import time
import webbrowser

# Always run from the project root so relative paths (data/, .env) resolve correctly
os.chdir(os.path.dirname(os.path.abspath(__file__)))

# Windows + psycopg compat. There are TWO things to do:
#
#   1. Set the policy so anything that respects it gets the right loop.
#   2. Override uvicorn 0.46's hardcoded ProactorEventLoop pick on Windows.
#      uvicorn/loops/asyncio.py:asyncio_loop_factory unconditionally
#      returns ProactorEventLoop on win32, which IGNORES our policy and
#      causes psycopg.pool to log:
#        "Psycopg cannot use the 'ProactorEventLoop' to run in async mode."
#      Monkey-patching the factory is the minimal-blast-radius fix.
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    try:
        import uvicorn.loops.asyncio as _ul
        def _selector_loop_factory(*_args, **_kwargs):
            return asyncio.SelectorEventLoop
        _ul.asyncio_loop_factory = _selector_loop_factory
    except ImportError:
        pass  # uvicorn not yet installed when this file is being lint-checked

# Load .env before any app module is imported (mirrors main.py behaviour)
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(name)s: %(message)s")

try:
    import uvicorn
except ImportError:
    print("uvicorn not found. Run:  pip install fastapi uvicorn[standard]")
    sys.exit(1)


def _open_browser(url: str, delay: float = 1.5) -> None:
    time.sleep(delay)
    webbrowser.open(url)


def main() -> None:
    parser = argparse.ArgumentParser(description="Research Assistant web server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--no-browser", action="store_true",
                        help="Do not open the browser automatically")
    args = parser.parse_args()

    url = f"http://{args.host}:{args.port}"
    print(f"\n  Research Assistant  →  {url}\n")

    if not args.no_browser:
        threading.Thread(target=_open_browser, args=(url,), daemon=True).start()

    uvicorn.run(
        "app.api.server:app",
        host=args.host,
        port=args.port,
        log_level="warning",
        reload=False,
    )


if __name__ == "__main__":
    main()
