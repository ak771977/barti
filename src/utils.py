import math
from typing import Optional


def round_up(value: float, step: float) -> float:
    if step <= 0:
        return value
    return math.ceil(value / step) * step


def round_down(value: float, step: float) -> float:
    if step <= 0:
        return value
    return math.floor(value / step) * step


def clamp(value: float, minimum: Optional[float] = None, maximum: Optional[float] = None) -> float:
    if minimum is not None:
        value = max(minimum, value)
    if maximum is not None:
        value = min(maximum, value)
    return value


def round_to_step(value: float, step: float) -> float:
    if step <= 0:
        return value
    return round(value / step) * step
