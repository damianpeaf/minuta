"""Spending cap for one transcription run.

Each request reserves its estimated cost before it leaves; if the reservation would pass the cap
the request is not sent and the run stops. What was already sent is paid anyway: the cap avoids
spending more, it cannot give money back.
"""

from __future__ import annotations

import threading


class BudgetExceeded(RuntimeError):
    pass


class Budget:
    def __init__(self, max_usd: float | None):
        self.max = max_usd
        self.spent = 0.0  # reserved estimates, corrected with real costs when reported
        self._lock = threading.Lock()

    def reserve(self, usd: float) -> None:
        with self._lock:
            if self.max is not None and self.spent + usd > self.max + 1e-9:
                raise BudgetExceeded(
                    f"the cap is US${self.max:.2f}: US${self.spent:.3f} used so far and the next request "
                    f"would cost ~US${usd:.3f}; stopping without sending it"
                )
            self.spent += usd

    def adjust(self, estimated: float, real: float | None) -> None:
        """Replace the reservation with what the provider says it charged, when it says."""
        if real is None:
            return
        with self._lock:
            self.spent += real - estimated


def estimate(seconds: float, usd_per_minute: float) -> float:
    return seconds / 60 * usd_per_minute
