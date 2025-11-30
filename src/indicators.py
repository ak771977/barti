from collections import deque
import math
from typing import Deque, Optional, Tuple


class BollingerBands:
    def __init__(self, period: int, stddev: float = 2.0) -> None:
        self.period = period
        self.stddev = stddev
        self.window: Deque[float] = deque(maxlen=period)
        self._sum = 0.0
        self._sumsq = 0.0

    def add(self, value: float) -> None:
        if len(self.window) == self.period:
            oldest = self.window.popleft()
            self._sum -= oldest
            self._sumsq -= oldest * oldest
        self.window.append(value)
        self._sum += value
        self._sumsq += value * value

    def ready(self) -> bool:
        return len(self.window) == self.period

    def bands(self) -> Optional[Tuple[float, float, float]]:
        if not self.ready():
            return None
        mean = self._sum / self.period
        variance = (self._sumsq / self.period) - (mean * mean)
        std = math.sqrt(max(variance, 0.0))
        upper = mean + self.stddev * std
        lower = mean - self.stddev * std
        return lower, mean, upper
