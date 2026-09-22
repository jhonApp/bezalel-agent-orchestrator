from __future__ import annotations

import argparse
import logging
import sys

from wake_listener.app import create_app
from wake_listener.config import WakeListenerSettings

logger = logging.getLogger(__name__)

_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


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
    if not settings.auth_token:
        logger.warning("WAKE_LISTENER_TOKEN is not set - this listener has NO authentication.")
        if settings.listen_host not in _LOOPBACK_HOSTS:
            print(
                "Refusing to start: WAKE_LISTENER_TOKEN must be set when binding to a "
                f"non-loopback host ({settings.listen_host}).",
                file=sys.stderr,
            )
            return 1
    import uvicorn
    uvicorn.run(create_app(settings), host=settings.listen_host, port=settings.listen_port)
    return 0


if __name__ == "__main__":
    raise SystemExit(cli())
