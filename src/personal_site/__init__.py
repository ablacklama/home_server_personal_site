from __future__ import annotations

import argparse

from .app import create_app
from .config import get_settings


def main() -> None:
    settings = get_settings()

    parser = argparse.ArgumentParser(prog="personal-site")
    parser.add_argument("--host", default=settings.host)
    parser.add_argument("--port", type=int, default=settings.port)
    parser.add_argument("--debug", action="store_true", default=settings.debug)
    args = parser.parse_args()

    app = create_app()
    app.run(host=args.host, port=args.port, debug=args.debug, use_reloader=args.debug)
