"""Exponential backoff with jitter for MQTT / DB reconnects."""

from __future__ import annotations

import random


class Backoff:
    def __init__(self, initial: float = 1.0, maximum: float = 60.0, factor: float = 2.0) -> None:
        self.initial, self.maximum, self.factor = initial, maximum, factor
        self.attempt = 0

    def next_delay(self) -> float:
        cap = min(self.maximum, self.initial * (self.factor**self.attempt))
        self.attempt += 1
        return random.uniform(cap / 2, cap)  # noqa: S311 - jitter, not crypto

    def reset(self) -> None:
        self.attempt = 0
