"""Entry point: ``python -m ingest_worker``."""

from __future__ import annotations

import asyncio
import sys

from ingest_worker.service import IngestService
from ingest_worker.settings import IngestSettings
from oee_shared.logging_setup import setup_logging


def main() -> None:
    setup_logging("ingest_worker")
    service = IngestService(IngestSettings())
    if sys.platform == "win32":  # paho/aiomqtt needs add_reader -> selector loop on Windows
        asyncio.run(service.run(), loop_factory=asyncio.SelectorEventLoop)
    else:
        asyncio.run(service.run())


if __name__ == "__main__":
    main()
