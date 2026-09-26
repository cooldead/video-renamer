"""Ripping statistics: transfer rates, drive speed multiples, time left.
Nothing here touches Qt."""

from __future__ import annotations

from collections import deque

# Data rate of a 1× drive, in bytes per second.
BLURAY_1X = 4_500_000   # 36 Mbit/s
DVD_1X = 1_385_000      # 11.08 Mbit/s


def speed_multiple(bytes_per_second: float, disc_kind: str) -> float:
    one_x = DVD_1X if "dvd" in disc_kind.lower() else BLURAY_1X
    return bytes_per_second / one_x


def format_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    hours, rest = divmod(seconds, 3600)
    minutes, seconds = divmod(rest, 60)
    return f"{hours}:{minutes:02}:{seconds:02}" if hours else f"{minutes}:{seconds:02}"


def format_rate(bytes_per_second: float) -> str:
    return f"{bytes_per_second / 1_000_000:.1f} MB/s"


class TransferMeter:
    """Bytes transferred over time: the current rate over a sliding window,
    and the average since the start."""

    def __init__(self, window: float = 10.0):
        self.window = window
        self._samples: deque[tuple[float, int]] = deque()
        self._first: tuple[float, int] | None = None

    def add(self, when: float, total_bytes: int) -> None:
        if self._first is None:
            self._first = (when, total_bytes)
        self._samples.append((when, total_bytes))
        while len(self._samples) > 2 and when - self._samples[0][0] > self.window:
            self._samples.popleft()

    @staticmethod
    def _rate(a: tuple[float, int], b: tuple[float, int]) -> float:
        elapsed = b[0] - a[0]
        return max(0.0, (b[1] - a[1]) / elapsed) if elapsed > 0 else 0.0

    def rate(self) -> float:
        """Current rate (bytes/s) over the last window."""
        return self._rate(self._samples[0], self._samples[-1]) if len(self._samples) > 1 else 0.0

    def average(self) -> float:
        return self._rate(self._first, self._samples[-1]) if self._first and self._samples else 0.0

    def remaining(self, total_bytes: int) -> float | None:
        """Seconds left to reach total_bytes at the current rate."""
        rate = self.rate()
        if not self._samples or rate <= 0:
            return None
        return max(0.0, (total_bytes - self._samples[-1][1]) / rate)
