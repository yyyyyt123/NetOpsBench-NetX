"""Fault execution helpers for scenario execution."""

from __future__ import annotations

from netopsbench.logging_utils import get_logger

logger = get_logger(__name__)


def inject_fault(runner, episode) -> dict:
    """Inject a fault via the centralized fault registry."""

    if episode.fault_type == "none":
        logger.info("\n[Fault Injection] No fault - baseline episode")
        return {"success": True, "fault_type": "none", "message": "Baseline episode"}

    logger.info(f"\n[Fault Injection] {episode.fault_type} on {episode.target_device}")

    spec = runner.fault_registry.get(episode.fault_type)
    if spec is None or spec.inject_episode is None:
        raise ValueError(f"Unsupported fault type: {episode.fault_type}")

    try:
        result = spec.inject_episode(runner.injector, episode)
    except RuntimeError as exc:
        logger.info(f"  ✗ Fault injection failed: {exc}")
        return {"success": False, "fault_type": episode.fault_type, "error": str(exc)}

    if result.get("success"):
        logger.info("  ✓ Fault injected successfully")
    else:
        logger.info(f"  ✗ Fault injection failed: {result.get('error')}")

    return result


def recover_fault(runner):
    """Recover all active faults."""

    logger.info("\n[Recovery] Recovering from faults...")
    results = runner.injector.recover_all()
    logger.info(f"  ✓ Recovered from {len(results)} faults")
    return results
