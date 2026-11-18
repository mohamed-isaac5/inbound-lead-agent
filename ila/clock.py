"""Clock with two modes: real time, or a manual simulated clock for deterministic demos/tests."""
import time
from typing import Optional


class Clock:
    def __init__(self):
        self._fixed: Optional[float] = None

    def now(self) -> float:
        return self._fixed if self._fixed is not None else time.time()

    @property
    def simulated(self) -> bool:
        return self._fixed is not None

    def set_fixed(self, epoch: Optional[float]):
        self._fixed = epoch

    def advance(self, seconds: float):
        if self._fixed is None:
            raise RuntimeError("clock is not simulated")
        self._fixed += seconds


clock = Clock()
