"""Entry point: python -m sensewatch"""

from __future__ import annotations

import logging


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    from .app import SenseWatchApp
    app = SenseWatchApp()
    app.run()


if __name__ == "__main__":
    main()
