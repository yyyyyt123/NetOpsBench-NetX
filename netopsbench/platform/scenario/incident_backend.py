"""ScenarioExecutor adapter for the shared incident engine."""

from __future__ import annotations

import time
from typing import Any

from netopsbench.models.scenario import ScenarioSpec
from netopsbench.platform.incident.context import (
    build_canonical_observation,
    build_public_case_id,
    build_public_symptoms,
    build_topology_snapshot,
    extract_episode_pingmesh_query_window,
)
from netopsbench.platform.incident.contracts import ToolAction
from netopsbench.platform.scenario.episode_runner import observe_episode
from netopsbench.platform.scenario.observation import (
    baseline_gate_errors,
    baseline_window_seconds,
    observation_integrity_errors,
)
from netopsbench.platform.toolkit.toolkit import AgentToolkit


class ExecutorIncidentBackend:
    """Low-level benchmark backend with no lifecycle state of its own."""

    def __init__(
        self,
        executor: Any,
        *,
        setup_traffic: bool = True,
        baseline_window: dict[str, Any] | None = None,
        influxdb_bucket: str | None = None,
        topology_id: str | None = None,
    ):
        self.executor = executor
        self.setup_traffic = setup_traffic
        self.baseline_window = baseline_window
        self.influxdb_bucket = influxdb_bucket
        self.topology_id = topology_id
        self.topology_dir = str(executor.topology_dir)
        self.scenario: ScenarioSpec | None = None
        self.episode_result: dict[str, Any] | None = None
        self.traffic_config: dict[str, Any] | None = None
        self.toolkit: Any = None
        self.topology: dict[str, Any] = {}
        self.symptoms: dict[str, Any] = {}
        self.pingmesh_query_window: dict[str, Any] = {}
        self.cleanup_result: dict[str, Any] | None = None
        self._finished = False
        self.baseline_preparation: dict[str, Any] = {"mode": "explicit"}
        self._scope = None

    def prepare(self, scenario: ScenarioSpec) -> dict[str, Any]:
        self.scenario = scenario
        if scenario.metadata.get("experiment") is not None:
            policy = self.executor.experiment
            window = policy.window_seconds(
                self.executor.current_manifest().require_pingmesh_plan().settings.complete_window_seconds
            )
            if (
                scenario.metadata["experiment"] != policy.model_dump(mode="json")
                or scenario.episode.duration_seconds != window
                or scenario.episode.stabilization_time
            ):
                raise ValueError("Campaign experiment configuration changed; regenerate scenarios in a new campaign")
            if scenario.episode.fault_type in {"link_flapping", "route_flapping"} and (
                scenario.episode.parameters.get("down_time") != policy.flapping_down_seconds
                or scenario.episode.parameters.get("up_time") != policy.flapping_up_seconds
            ):
                raise ValueError("Per-case periodic timing override is not allowed in a frozen campaign")
        if self.setup_traffic:
            with self.executor.timings.measure("traffic_readiness"):
                controller = self.executor.traffic_controller
                traffic_ready = (
                    controller is not None and bool(controller.active_flows) and controller.verify_active_flows()
                )
                if not traffic_ready:
                    if controller is not None:
                        self.executor._stop_traffic()
                    self.traffic_config = self.executor._setup_traffic(
                        scenario.scale,
                        scenario.traffic_profile,
                    )
                if traffic_ready:
                    self.traffic_config = getattr(self.executor, "_traffic_config", None)
            handoff = getattr(self.executor, "_baseline_handoff", None)
            if handoff is not None and self.baseline_window is None:
                self._scope = self.executor._baseline_scope(scenario)
                self.baseline_window, reason = handoff.take(
                    self._scope, baseline_window_seconds(self.executor, self.executor.minimum_baseline_seconds)
                )
                self.baseline_preparation = {
                    "mode": "reused" if self.baseline_window else "fresh",
                    "reason": reason,
                    "source_scenario_id": handoff.source if self.baseline_window else None,
                    "scope_changes": handoff.last_changes,
                }
            if self.baseline_window is None:
                if self.executor.runtime_worker is not None:
                    from .observation import observation_detector

                    with self.executor.timings.measure(
                        "baseline_readiness", timeout_seconds=self.executor.observation_timeout_seconds
                    ):
                        observation_detector(self.executor).wait_for_history(
                            deadline=time.monotonic() + self.executor.observation_timeout_seconds
                        )
                with self.executor.timings.measure(
                    "baseline",
                    planned_seconds=baseline_window_seconds(self.executor, self.executor.minimum_baseline_seconds),
                ):
                    reference = self.executor._capture_baseline_window()
                with self.executor.timings.measure(
                    "baseline_query", timeout_seconds=self.executor.observation_timeout_seconds
                ):
                    validation = self.executor._merge_observation_windows(
                        [reference], int(reference["duration_seconds"]), baseline_window=reference
                    )
                gate_errors = baseline_gate_errors(validation)
                if gate_errors:
                    self.baseline_preparation["failed_validation"] = validation
                    raise RuntimeError("Healthy baseline is unavailable: " + "; ".join(gate_errors))
                self.baseline_window = {
                    "name": "baseline",
                    "start_time": validation["start_time"],
                    "end_time": validation["end_time"],
                    "duration_seconds": validation["duration_seconds"],
                }
                if handoff is not None:
                    handoff.fresh_epoch()
            else:
                self.executor.timings.skip("baseline", "verified healthy window handoff")
        if self.baseline_window is None:
            raise RuntimeError("Incident preparation requires an explicit Pingmesh baseline window")
        self.executor._case_initial_scope = self._scope
        catalog_v2 = scenario.metadata.get("fault_catalog_version") == 2
        self.executor._case_affected_pairs = []
        self.executor._case_probe_pairs = []
        if catalog_v2 and not scenario.episode.is_healthy and scenario.episode.parameters.get("affected_candidates"):
            from netopsbench.platform.utils.interface_names import to_sonic_interface

            from .candidates import CandidatePaths

            episode = scenario.episode
            manifest = self.executor.current_manifest()
            scope = (id(manifest), manifest.topology_id, manifest.require_pingmesh_plan().settings.plan_id)
            if getattr(self.executor, "_candidate_scope", None) != scope:
                self.executor._candidate_paths = CandidatePaths(manifest)
                self.executor._candidate_scope = scope
            candidates = self.executor._candidate_paths
            peer = next(
                (
                    peer
                    for (root, peer), interface in candidates.ports.items()
                    if root == episode.target_device and interface == to_sonic_interface(episode.target_interface)
                ),
                None,
            )
            self.executor._case_affected_pairs = candidates.affected_pairs(
                episode.fault_type,
                episode.target_device,
                peer,
                episode.parameters["affected_candidates"][0],
                reverse=(
                    episode.fault_type == "route_flapping" or episode.parameters.get("mechanism") == "export_filter"
                ),
            )
            self.executor._case_probe_pairs = self.executor._case_affected_pairs + episode.parameters.get(
                "control_candidates", []
            )
        if catalog_v2:
            from netopsbench.platform.observability.routes import collect_routes

            with self.executor.timings.measure(
                "baseline_routes",
                timeout_seconds=self.executor.scale_registry.get(scenario.scale).health_timeout_seconds,
            ):
                self.executor._case_routes = collect_routes(
                    self.executor.current_manifest(),
                    cancel_event=getattr(self.executor, "_cancel_event", None),
                    deadline=time.monotonic() + self.executor.scale_registry.get(scenario.scale).health_timeout_seconds,
                )
            if any(
                row.get("error") or row.get("bgp_collection_ok") is False
                for row in self.executor._case_routes["nodes"].values()
            ):
                raise RuntimeError("Healthy route snapshot is unavailable")
        self.episode_result = {}
        self.episode_result = observe_episode(
            self.executor,
            scenario.episode,
            baseline_window=self.baseline_window,
            result=self.episode_result,
        )
        if catalog_v2:
            from netopsbench.platform.observability.routes import collect_routes, route_facts

            from .effects import scoped_integrity_errors, validate_effect

            with self.executor.timings.measure(
                "effect_validation",
                timeout_seconds=self.executor.scale_registry.get(scenario.scale).health_timeout_seconds,
            ):
                current = collect_routes(
                    self.executor.current_manifest(),
                    cancel_event=getattr(self.executor, "_cancel_event", None),
                    deadline=time.monotonic() + self.executor.scale_registry.get(scenario.scale).health_timeout_seconds,
                )
                self.episode_result["route_evidence"] = {"baseline": self.executor._case_routes, "fault": current}
                self.episode_result["observations"]["route_observations"] = route_facts(
                    self.executor._case_routes, current, self.executor.current_manifest()
                )
                effect = validate_effect(
                    self.executor, scenario.episode, self.episode_result, self.executor._case_routes, current
                )
                self.episode_result["effect_audit"] = effect
                integrity_errors = scoped_integrity_errors(
                    self.episode_result.get("observations") or {},
                    self.episode_result.get("coverage_audit") or {},
                    effect,
                )
                integrity_errors.extend(effect["errors"])
        else:
            integrity_errors = observation_integrity_errors(self.episode_result.get("observations") or {})
        if integrity_errors:
            raise RuntimeError("Incident observation is incomplete: " + "; ".join(integrity_errors))
        self.toolkit = AgentToolkit(
            topology_dir=self.topology_dir,
            topology_metadata=self.executor.topology_metadata,
        )
        self.toolkit.influxdb_bucket = self.influxdb_bucket or self.executor.influxdb_bucket
        self.toolkit.topology_id = self.topology_id or self.executor.topology_id
        self.pingmesh_query_window = extract_episode_pingmesh_query_window(self.episode_result)
        self.toolkit.set_pingmesh_time_window(
            self.pingmesh_query_window.get("start_time"),
            self.pingmesh_query_window.get("end_time"),
        )
        self.topology = build_topology_snapshot(self.toolkit)
        self.symptoms = build_public_symptoms(
            episode_result=self.episode_result,
            pingmesh_query_window=self.pingmesh_query_window,
        )
        return build_canonical_observation(
            case_id=build_public_case_id(
                scenario_id=scenario.id,
                episode_result=self.episode_result,
            ),
            topology=self.topology,
            symptoms=self.symptoms,
        )

    def call_tool(self, action: ToolAction) -> dict[str, Any]:
        if self.toolkit is None:
            raise RuntimeError("Incident backend is not prepared")
        method = getattr(self.toolkit, action.name, None)
        if not callable(method):
            raise ValueError(f"Toolkit action is not implemented: {action.name}")
        result = method(**action.arguments)
        return result.to_dict() if hasattr(result, "to_dict") else {"success": True, "data": result}

    def refresh(self, *, min_seconds: float = 0.0) -> None:
        del min_seconds

    def finish(self, *, broken: bool = False) -> None:
        if self._finished:
            return
        self._finished = True
        if self.scenario is not None:
            self.executor._recovering = True
            try:
                with self.executor.timings.measure(
                    "recovery", timeout_seconds=self.executor.experiment.recovery_timeout_seconds
                ):
                    self.cleanup_result = self.executor._cleanup_after_scenario(
                        self.scenario,
                        self.episode_result,
                        baseline_window=self.baseline_window,
                    )
                    if not self.cleanup_result.get("success"):
                        self.executor.timings.phases["recovery"]["status"] = "failed"
            finally:
                self.executor._recovering = False
            if self.episode_result is not None and "recovery" in self.cleanup_result:
                self.episode_result["recovery"] = self.cleanup_result["recovery"]
            if "validated_baseline" in self.cleanup_result:
                self.baseline_window = dict(self.cleanup_result["validated_baseline"])
            handoff = getattr(self.executor, "_baseline_handoff", None)
            if handoff is not None and self.setup_traffic:
                candidate = self.cleanup_result.get("validated_baseline")
                if candidate is None and self.scenario.episode.is_healthy:
                    candidate = self.executor._healthy_baseline_candidate
                scope = self.executor._baseline_scope(self.scenario)
                if (
                    not broken
                    and self.cleanup_result.get("success")
                    and candidate is not None
                    and self._scope is not None
                    and scope == self.cleanup_result.get("validated_baseline_scope", self._scope)
                ):
                    self.cleanup_result["baseline_handoff"] = handoff.publish(
                        candidate,
                        scope,
                        self.scenario.id,
                        baseline_window_seconds(self.executor, self.executor.minimum_baseline_seconds),
                    )
                else:
                    handoff.clear()
                    self.cleanup_result["baseline_handoff"] = False
                from .baseline import scope_changes

                self.cleanup_result["baseline_scope_changes"] = scope_changes(self._scope, scope)
                from .baseline import RuntimeScope

                bound = self.cleanup_result.get("validated_baseline_scope")
                if isinstance(bound, RuntimeScope):
                    self.cleanup_result["validated_baseline_scope"] = {"digest": bound.digest}
            if not self.cleanup_result.get("success", False):
                raise RuntimeError(f"Scenario cleanup failed: {self.cleanup_result}")


__all__ = ["ExecutorIncidentBackend"]
