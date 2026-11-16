"""Container healthcheck: the flush loop touches a heartbeat file at least every batch wait."""

from __future__ import annotations

import sys
import time
from pathlib import Path

from ingest_worker.settings import IngestSettings


def main() -> int:
    s = IngestSettings()
    try:
        age = time.time() - Path(s.heartbeat_file).stat().st_mtime
    except OSError:
        print("no heartbeat yet")
        return 1
    if age > s.heartbeat_max_age_s:
        print(f"heartbeat stale ({age:.0f}s)")
        return 1
    print(f"ok ({age:.1f}s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
