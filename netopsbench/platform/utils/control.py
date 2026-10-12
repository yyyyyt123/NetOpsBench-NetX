"""Cancellation and elapsed-time budgets for internal runtime operations."""

from __future__ import annotations

import time
from dataclasses import dataclass
from threading import Event
from typing import overload


class OperationCancelled(RuntimeError):
    """The caller requested cancellation before the next side effect."""


@dataclass(frozen=True)
class OperationControl:
    cancel_event: Event | None = None
    deadline: float | None = None

    @overload
    def remaining(self, maximum: float) -> float: ...

    @overload
    def remaining(self, maximum: None = None) -> float | None: ...

    def remaining(self, maximum: float | None = None) -> float | None:
        if self.cancel_event is not None and self.cancel_event.is_set():
            raise OperationCancelled("Operation cancelled")
        if self.deadline is None:
            return maximum
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Operation deadline exceeded")
        return remaining if maximum is None else min(remaining, maximum)

    def sleep(self, seconds: float) -> None:
        duration = self.remaining(seconds)
        if self.cancel_event is None:
            time.sleep(duration)
        else:
            self.cancel_event.wait(duration)
        self.remaining()
