import argparse
import asyncio
import logging
import socket

from . import __version__, server


class DryRunHub:
    """Stand-in for InputHub that only logs what would be injected."""

    def __getattr__(self, name):
        log = logging.getLogger("dry-run")

        class _Any:
            def __getattr__(self, m):
                return lambda *a, **k: log.info("%s.%s%s", name, m, a)

            def __call__(self, *a, **k):
                log.info("%s%s", name, a)
                return self

        return _Any()


def main() -> None:
    ap = argparse.ArgumentParser(prog="monect-receiver",
                                 description="Monect PC Remote receiver for Linux")
    ap.add_argument("--name", default=socket.gethostname(), help="name shown on the phone")
    ap.add_argument("--debug", action="store_true", help="verbose logging (SDP, candidates)")
    ap.add_argument("--dry-run", action="store_true", help="log input instead of injecting it")
    ap.add_argument("--version", action="version", version=__version__)
    a = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if a.debug else logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s",
                        datefmt="%H:%M:%S")
    for noisy in ("aioice", "aiortc", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    if a.dry_run:
        server.InputHub = DryRunHub  # type: ignore[assignment]
    try:
        asyncio.run(server.run(a.name))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
