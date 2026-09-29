"""Entry point: `uv run evalbench [--port N] [--no-browser]`."""

from __future__ import annotations

import argparse
import logging
import threading
import webbrowser

import uvicorn

from .app import create_app
from .config import load_settings


def main() -> None:
    parser = argparse.ArgumentParser(prog="evalbench", description=__doc__)
    parser.add_argument("--host", help="bind address (default 127.0.0.1; agents run with your credentials, keep it local)")
    parser.add_argument("--port", type=int)
    parser.add_argument("--no-browser", action="store_true", help="don't open a browser tab")
    args = parser.parse_args()

    settings = load_settings()
    if args.host:
        settings.host = args.host
    if args.port:
        settings.port = args.port

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    url = f"http://{settings.host}:{settings.port}"
    print(f"evalbench → {url}   (data: {settings.data_dir}, workspaces: {settings.workspace_root})")
    if not args.no_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port, log_level="warning")


if __name__ == "__main__":
    main()
