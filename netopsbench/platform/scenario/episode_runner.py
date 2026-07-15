"""Per-episode execution for :class:`ScenarioExecutor`."""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from netopsbench.logging_utils import get_logger

from .models import Episode

logger = get_logger(__name__)


def run_episode(
    executor: Any,
    episode: Episode,
    diagnosis_callback: Callable[[dict], dict] | None = None,
    diagnose_if_skipped: bool = False,
) -> dict:
    """Execute one episode using the owning scenario executor."""
    logger.info(f"\n{'=' * 70}")
    logger.info(f"Episode: {episode.episode_id}")
    logger.info(f"Description: {episode.description}")
    logger.info(f"{'=' * 70}")

    episode_result: dict[str, Any] = {
        "episode_id": episode.episode_id,
        "description": episode.description,
        "episode": _episode_payload(episode),
        "start_time": datetime.now(UTC).isoformat(),
        "success": False,
        "error": None,
    }

    try:
        if executor.skip_none_episodes and episode.fault_type == "none":
            logger.info("\n[Episode] Skipping baseline fault actions (fault_type=none)")
            skipped_start = datetime.now(UTC).isoformat()
            waited_seconds = max(0, int(episode.duration_seconds or 0))
            if waited_seconds > 0:
                logger.info(f"[Episode] Waiting {waited_seconds}s to reserve a clean pre-fault baseline window...")
                if diagnose_if_skipped:
                    skipped_observations = executor._wait_and_observe(waited_seconds)
                else:
                    _sleep(executor, waited_seconds)
                    skipped_observations = None
            else:
                skipped_observations = None
            skipped_end = datetime.now(UTC).isoformat()
            skipped_result = executor._build_skipped_episode_result(
                episode,
                start_time=skipped_start,
                end_time=skipped_end,
                waited_seconds=waited_seconds,
            )
            if skipped_observations is not None:
                skipped_result["observations"] = skipped_observations
            if diagnosis_callback and diagnose_if_skipped:
                logger.info("\n[Diagnosis] Calling agent on healthy-network episode (negative sample)...")
                try:
                    skipped_result["diagnosis"] = diagnosis_callback(skipped_result)
                except Exception as diagnosis_error:  # noqa: BLE001
                    skipped_result["diagnosis"] = {
                        "error": str(diagnosis_error),
                        "success": False,
                    }
            return skipped_result

        pre_fault_reference = datetime.now(UTC).replace(microsecond=0)
        injection_result = executor._inject_fault(episode)
        episode_result["injection"] = injection_result
        if not injection_result.get("success"):
            episode_result["error"] = "Fault injection failed"
            return episode_result

        early_observation_seconds = int(
            episode.metadata.get(
                "early_observation_seconds",
                min(20, max(10, episode.duration_seconds // 3)),
            )
        )
        if early_observation_seconds >= episode.duration_seconds:
            early_observation_seconds = max(0, episode.duration_seconds - 10)
        steady_observation_seconds = max(1, episode.duration_seconds - early_observation_seconds)

        observation_windows: list[dict] = []
        if early_observation_seconds > 0:
            logger.info(f"\n[Early Observation] Monitoring immediately for {early_observation_seconds} seconds...")
            observation_windows.append(executor._capture_observation_window(early_observation_seconds, "early"))

        logger.info(f"\n[Stabilization] Waiting {episode.stabilization_time}s...")
        _sleep(executor, episode.stabilization_time)

        logger.info(f"\n[Steady Observation] Monitoring stabilized fault for {steady_observation_seconds} seconds...")
        observation_windows.append(executor._capture_observation_window(steady_observation_seconds, "steady"))

        episode_result["observations"] = executor._merge_observation_windows(
            observation_windows,
            total_duration_seconds=episode.duration_seconds,
            baseline_end_time=pre_fault_reference,
        )
        coverage_audit = episode_result["observations"].pop("_coverage_audit", None)

        if diagnosis_callback and episode.fault_type != "none":
            try:
                episode_result["diagnosis"] = diagnosis_callback(episode_result)
            except Exception as diagnosis_error:  # noqa: BLE001
                episode_result["diagnosis"] = {
                    "error": str(diagnosis_error),
                    "success": False,
                }

        if coverage_audit is not None:
            episode_result["coverage_audit"] = coverage_audit

        episode_result["recovery"] = executor._recover_fault()
        logger.info(f"\n[Post-Recovery] Waiting {executor.post_recovery_wait_seconds}s for recovery...")
        _sleep(executor, executor.post_recovery_wait_seconds)

        episode_result["success"] = True
        episode_result["end_time"] = datetime.now(UTC).isoformat()
        return episode_result

    except Exception as exc:  # noqa: BLE001
        logger.info(f"\nEpisode failed: {exc}")
        episode_result["error"] = str(exc)
        episode_result["end_time"] = datetime.now(UTC).isoformat()
        try:
            executor._recover_fault()
        except Exception as recovery_error:  # noqa: BLE001
            logger.info(f"Warning: Recovery failed: {recovery_error}")
        return episode_result


def _episode_payload(episode: Episode) -> dict[str, Any]:
    return {
        "episode_id": episode.episode_id,
        "fault_type": episode.fault_type,
        "target_device": episode.target_device,
        "target_interface": episode.target_interface,
        "target_prefix": episode.target_prefix,
        "mtu": episode.mtu,
        "duration_seconds": episode.duration_seconds,
        "stabilization_time": episode.stabilization_time,
        "metadata": episode.metadata,
        "parameters": episode.parameters,
    }


def _sleep(executor: Any, seconds: float) -> None:
    getattr(executor, "sleep", time.sleep)(seconds)


__all__ = ["run_episode"]
