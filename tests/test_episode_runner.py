"""Tests for per-episode scenario execution."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from netopsbench.platform.scenario.episode_runner import run_episode
from netopsbench.platform.scenario.models import Episode


class StubExecutor:
    """Minimal ScenarioExecutor stand-in for exercising run_episode()."""

    def __init__(
        self,
        *,
        skip_none_episodes: bool = True,
        post_recovery_wait_seconds: float = 0,
        injection_success: bool = True,
        observe_payload: dict[str, Any] | None = None,
    ):
        self.skip_none_episodes = skip_none_episodes
        self.post_recovery_wait_seconds = post_recovery_wait_seconds
        self._injection_success = injection_success
        self._observe_payload = observe_payload or {"observed": True}
        self.calls: list[str] = []

    def _build_skipped_episode_result(self, episode, *, start_time, end_time, waited_seconds):
        self.calls.append("skipped")
        return {
            "episode_id": episode.episode_id,
            "skipped": True,
            "waited_seconds": waited_seconds,
            "start_time": start_time,
            "end_time": end_time,
            "success": True,
        }

    def _inject_fault(self, episode):
        self.calls.append("inject")
        return {"success": self._injection_success, "fault_type": episode.fault_type}

    def _wait_and_observe(self, seconds, *, baseline_end_time=None):
        self.calls.append(f"observe:{seconds}")
        return {"duration": seconds, **self._observe_payload}

    def _capture_observation_window(self, seconds, name):
        self.calls.append(f"capture:{name}:{seconds}")
        return {"name": name, "duration": seconds, **self._observe_payload}

    def _merge_observation_windows(self, windows, *, total_duration_seconds, baseline_end_time=None):
        self.calls.append("merge")
        return {"windows": list(windows), "total": total_duration_seconds}

    def _recover_fault(self):
        self.calls.append("recover")
        return {"success": True}


def _make_episode(**overrides) -> Episode:
    base = dict(
        episode_id="ep-1",
        description="test episode",
        fault_type="link_down",
        target_device="leaf-1",
        target_interface="Ethernet1",
        target_prefix=None,
        mtu=None,
        duration_seconds=12,
        stabilization_time=0,
        metadata={"early_observation_seconds": 1},
        parameters={},
    )
    base.update(overrides)
    return Episode(**base)


def test_runner_skips_baseline_when_fault_type_none():
    executor = StubExecutor(skip_none_episodes=True)
    episode = _make_episode(fault_type="none", duration_seconds=0, metadata={})
    result = run_episode(executor, episode)
    assert result["skipped"] is True
    assert "inject" not in executor.calls
    assert executor.calls == ["skipped"]
    assert datetime.fromisoformat(result["start_time"]).tzinfo is not None
    assert datetime.fromisoformat(result["end_time"]).tzinfo is not None


def test_runner_returns_error_when_injection_fails():
    executor = StubExecutor(injection_success=False)
    episode = _make_episode(duration_seconds=2, metadata={"early_observation_seconds": 0})
    result = run_episode(executor, episode)
    assert result["success"] is False
    assert result["error"] == "Fault injection failed"
    assert "recover" not in executor.calls


def test_runner_full_happy_path_invokes_inject_observe_recover():
    executor = StubExecutor()
    episode = _make_episode(duration_seconds=2, metadata={"early_observation_seconds": 0})
    result = run_episode(executor, episode)
    assert result["success"] is True
    assert "injection" in result
    assert "observations" in result
    assert "recovery" in result
    assert executor.calls == ["inject", "capture:steady:2", "merge", "recover"]


def test_runner_defers_detector_analysis_until_all_observation_windows_are_captured():
    executor = StubExecutor()
    executor.sleep = lambda _seconds: None
    episode = _make_episode(duration_seconds=12, stabilization_time=2, metadata={"early_observation_seconds": 4})

    result = run_episode(executor, episode)

    assert result["success"] is True
    assert executor.calls == [
        "inject",
        "capture:early:4",
        "capture:steady:8",
        "merge",
        "recover",
    ]
    assert all(not call.startswith("observe:") for call in executor.calls)


def test_runner_uses_executor_sleep_hook():
    executor = StubExecutor(post_recovery_wait_seconds=2)
    slept = []
    executor.sleep = slept.append
    episode = _make_episode(duration_seconds=3, stabilization_time=1, metadata={"early_observation_seconds": 0})

    result = run_episode(executor, episode)

    assert result["success"] is True
    assert slept == [1, 2]


def test_runner_invokes_diagnosis_callback_for_fault_episodes():
    executor = StubExecutor()
    episode = _make_episode(duration_seconds=2, metadata={"early_observation_seconds": 0})

    captured: list[dict[str, Any]] = []

    def diagnose(payload):
        captured.append(payload)
        return {"verdict": "ok", "success": True}

    result = run_episode(executor, episode, diagnosis_callback=diagnose)
    assert result["diagnosis"] == {"verdict": "ok", "success": True}
    assert captured, "diagnosis callback should have been invoked"


def test_runner_does_not_invoke_diagnosis_for_baseline_episode():
    executor = StubExecutor(skip_none_episodes=False)
    episode = _make_episode(
        fault_type="none",
        duration_seconds=2,
        metadata={"early_observation_seconds": 0},
    )

    called = []
    run_episode(executor, episode, diagnosis_callback=lambda payload: called.append(payload) or {})
    assert called == []


def test_runner_captures_diagnosis_exception_into_result():
    executor = StubExecutor()
    episode = _make_episode(duration_seconds=2, metadata={"early_observation_seconds": 0})

    def boom(_payload):
        raise RuntimeError("diagnose failed")

    result = run_episode(executor, episode, diagnosis_callback=boom)
    assert result["diagnosis"] == {"error": "diagnose failed", "success": False}
    assert result["success"] is True


def test_runner_attempts_recovery_on_unexpected_exception():
    class ExplodingExecutor(StubExecutor):
        def _capture_observation_window(self, seconds, name):
            raise RuntimeError("observe blew up")

    executor = ExplodingExecutor()
    episode = _make_episode(duration_seconds=2, metadata={"early_observation_seconds": 0})
    result = run_episode(executor, episode)
    assert result["success"] is False
    assert "observe blew up" in result["error"]
    assert "recover" in executor.calls
