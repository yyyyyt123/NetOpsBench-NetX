"""Internal helpers for session execution."""

from .types import ScenarioExecutionRef

__all__ = ["SessionOrchestrator", "ScenarioExecutionRef"]


def __getattr__(name: str):
    if name == "SessionOrchestrator":
        from .orchestrator import SessionOrchestrator

        return SessionOrchestrator
    raise AttributeError(name)
