from __future__ import annotations

import argparse

from wake_listener.app import create_app
from wake_listener.config import WakeListenerSettings


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="bezalel-wake-listener")
    p.add_argument("--host", default=None, help="override WAKE_LISTENER_HOST")
    p.add_argument("--port", type=int, default=None, help="override WAKE_LISTENER_PORT")
    return p


def cli(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    settings = WakeListenerSettings.load()
    if args.host:
        settings.listen_host = args.host
    if args.port:
        settings.listen_port = args.port
    import uvicorn
    uvicorn.run(create_app(settings), host=settings.listen_host, port=settings.listen_port)
    return 0


if __name__ == "__main__":
    raise SystemExit(cli())
