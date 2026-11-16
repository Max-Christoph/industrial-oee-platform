"""Uniform, container-friendly logging (single line, UTC, level from LOG_LEVEL)."""

from __future__ import annotations

import logging
import os
import time


def setup_logging(service: str) -> None:
    level = os.environ.get("LOG_LEVEL", "INFO").upper()
    fmt = f"%(asctime)sZ %(levelname)s {service} %(name)s: %(message)s"
    logging.basicConfig(level=level, format=fmt, datefmt="%Y-%m-%dT%H:%M:%S", force=True)
    logging.Formatter.converter = time.gmtime
    for noisy in ("httpx", "httpcore", "psycopg.pool"):
        logging.getLogger(noisy).setLevel(max(logging.WARNING, logging.getLevelName(level)))
