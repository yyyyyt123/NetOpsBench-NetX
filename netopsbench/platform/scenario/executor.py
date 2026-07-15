"""
Scenario Runner - Executes automated fault injection scenarios

Inspired by impl_plan.md concepts: scenarios, episodes, and manifests
"""

import json
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from netopsbench.logging_utils import get_logger
from netopsbench.platform.faults.injector import FaultInjector
from netopsbench.platform.faults.scenario_execution import inject_fault as _inject_fault_impl
from netopsbench.platform.faults.scenario_execution import recover_fault as _recover_fault_impl
from netopsbench.platform.faults.specs import FaultSpecRegistry, create_fault_registry
from netopsbench.platform.topology.topology_utils import coerce_topology_manifest, load_topology_manifest
from netopsbench.platform.traffic.controller import TrafficController
from netopsbench.platform.traffic.scenario_execution import setup_traffic as _setup_traffic_impl
from netopsbench.platform.traffic.scenario_execution import stop_traffic as _stop_traffic_impl

from .episode_runner import run_episode
from .models import Episode, Scenario
from .observation import analyze_observation_windows as _analyze_observation_windows_impl
from .observation import capture_observation_window as _capture_observation_window_impl
from .observation import wait_and_observe as _wait_and_observe_impl

logger = get_logger(__name__)


class ScenarioExecutor:
    """
    Orchestrates execution of test scenarios with automated fault injection.
    """

    def __init__(
        self,
        topology_dir: str = "clab-topology",
        topology_metadata: dict | None = None,
        baseline_wait_seconds: int = 60,
        post_recovery_wait_seconds: int = 2,
        skip_none_episodes: bool = False,
        influxdb_url: str | None = None,
        influxdb_token: str | None = None,
        influxdb_org: str | None = None,
        influxdb_bucket: str | None = None,
        topology_id: str | None = None,
        sleep_fn: Callable[[float], None] | None = None,
        persist_results: bool = True,
        fault_registry: FaultSpecRegistry | None = None,
    ):
        """
        Initialize scenario runner.

        Args:
            topology_dir: Directory containing topology files
        """
        self.topology_dir = topology_dir
        metadata = topology_metadata
        if metadata is None:
            topology_file = Path(topology_dir) / "topology.json"
            if topology_file.exists():
                metadata = load_topology_manifest(topology_file).model_dump(mode="json")
        if metadata is None:
            raise ValueError(f"Canonical topology metadata is required for ScenarioExecutor: {topology_dir}")
        manifest = coerce_topology_manifest(metadata)
        self.fault_registry = fault_registry or create_fault_registry()
        self.topology_metadata = manifest.model_dump(mode="json")
        self.injector = FaultInjector(
            clab_dir=topology_dir,
            topology_metadata=self.topology_metadata,
            fault_registry=self.fault_registry,
        )
        self.traffic_controller: TrafficController | None = None
        self.results_dir = Path("scenario_results")
        self.topology_id = topology_id or manifest.topology_id
        self.influxdb_url = influxdb_url
        self.influxdb_token = influxdb_token
        self.influxdb_org = influxdb_org
        self.influxdb_bucket = influxdb_bucket
        self.baseline_wait_seconds = max(0, int(baseline_wait_seconds))
        self.post_recovery_wait_seconds = max(0, int(post_recovery_wait_seconds))
        self.skip_none_episodes = bool(skip_none_episodes)
        self._sleep_fn = sleep_fn or time.sleep
        self.persist_results = bool(persist_results)

    def sleep(self, seconds: float) -> None:
        self._sleep_fn(seconds)

    def _setup_traffic(self, scale: str, profile: str) -> dict:
        return _setup_traffic_impl(self, scale, profile)

    def _stop_traffic(self):
        _stop_traffic_impl(self)

    def _inject_fault(self, episode: Episode) -> dict:
        return _inject_fault_impl(self, episode)

    def _wait_and_observe(self, duration: int, baseline_end_time: datetime | None = None) -> dict:
        return _wait_and_observe_impl(self, duration, baseline_end_time)

    def _capture_observation_window(self, duration: int, name: str) -> dict:
        return _capture_observation_window_impl(self, duration, name=name)

    def _recover_fault(self):
        return _recover_fault_impl(self)

    def _merge_observation_windows(
        self,
        windows: list[dict],
        total_duration_seconds: int,
        baseline_end_time: datetime | None = None,
    ) -> dict:
        return _analyze_observation_windows_impl(
            self,
            windows,
            total_duration_seconds,
            baseline_end_time=baseline_end_time,
        )

    def _build_skipped_episode_result(
        self, episode: Episode, start_time: str | None = None, end_time: str | None = None, waited_seconds: int = 0
    ) -> dict:
        now = datetime.now(UTC).isoformat()
        return {
            "episode_id": episode.episode_id,
            "description": episode.description,
            "episode": {
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
            },
            "start_time": start_time or now,
            "end_time": end_time or now,
            "success": True,
            "skipped": True,
            "skip_reason": "fault_type_none_in_benchmark",
            "waited_seconds": max(0, int(waited_seconds or 0)),
        }

    def run_episode(self, episode: Episode, diagnosis_callback=None, diagnose_if_skipped: bool = False) -> dict:
        """Run a single episode."""
        return run_episode(
            self,
            episode,
            diagnosis_callback=diagnosis_callback,
            diagnose_if_skipped=diagnose_if_skipped,
        )

    def run_scenario(self, scenario: Scenario, diagnosis_callback=None) -> dict:
        """
        Run complete scenario with all episodes.

        Args:
            scenario: Scenario specification

        Returns:
            Scenario result dict
        """
        logger.info(f"\n{'#'*70}")
        logger.info(f"# Scenario: {scenario.name}")
        logger.info(f"# ID: {scenario.scenario_id}")
        logger.info(f"# Description: {scenario.description}")
        logger.info(f"# Topology: {scenario.topology_scale}")
        logger.info(f"# Traffic Profile: {scenario.traffic_profile}")
        logger.info(f"# Episodes: {len(scenario.episodes)}")
        logger.info(f"{'#'*70}")

        scenario_result = {
            "scenario_id": scenario.scenario_id,
            "name": scenario.name,
            "start_time": datetime.now(UTC).isoformat(),
            "topology_scale": scenario.topology_scale,
            "traffic_profile": scenario.traffic_profile,
            "episodes": [],
            "success": False,
        }

        try:
            # Setup traffic
            traffic_config = self._setup_traffic(scenario.topology_scale, scenario.traffic_profile)
            scenario_result["traffic_config"] = traffic_config

            # Wait for traffic to stabilize
            logger.info(f"\n[Baseline] Waiting {self.baseline_wait_seconds}s for traffic baseline...")
            self.sleep(self.baseline_wait_seconds)

            # Run each episode
            is_negative_sample = bool((scenario.metadata or {}).get("negative_sample", False))
            n_episodes = len(scenario.episodes)
            for i, episode in enumerate(scenario.episodes, 1):
                logger.info(f"\n[Episode {i}/{n_episodes}]")
                # For negative-sample scenarios, diagnose the middle episode so the
                # agent observes a representative healthy window (false-positive check).
                diagnose_if_skipped = is_negative_sample and (i - 1) == n_episodes // 2
                episode_result = self.run_episode(
                    episode,
                    diagnosis_callback=diagnosis_callback,
                    diagnose_if_skipped=diagnose_if_skipped,
                )
                scenario_result["episodes"].append(episode_result)

                if not episode_result.get("success"):
                    logger.info(f"\n✗ Episode {episode.episode_id} failed, continuing...")

            # Mark scenario as successful if all episodes completed
            scenario_result["success"] = all(ep.get("success", False) for ep in scenario_result["episodes"])

        except Exception as e:
            logger.info(f"\n✗ Scenario failed: {e}")
            scenario_result["error"] = str(e)

        finally:
            # Always stop traffic and ensure recovery
            self._stop_traffic()
            self._recover_fault()

            scenario_result["end_time"] = datetime.now(UTC).isoformat()

        if self.persist_results:
            result_file = self._persist_scenario_result(scenario, scenario_result)
            scenario_result["result_file"] = str(result_file)
        else:
            result_file = scenario_result.get("result_file")

        logger.info(f"\n{'#'*70}")
        logger.info("# Scenario Complete")
        logger.info(f"# Success: {scenario_result['success']}")
        if result_file:
            logger.info(f"# Results saved to: {result_file}")
        logger.info(f"{'#'*70}")

        return scenario_result

    def _persist_scenario_result(self, scenario: Scenario, scenario_result: dict) -> Path:
        self.results_dir.mkdir(parents=True, exist_ok=True)
        result_file = self.results_dir / f"{scenario.scenario_id}_{datetime.now(UTC).strftime('%Y%m%d_%H%M%S')}.json"
        with open(result_file, "w", encoding="utf-8") as f:
            json.dump(scenario_result, f, indent=2)
        return result_file
