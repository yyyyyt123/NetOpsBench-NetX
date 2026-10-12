#!/usr/bin/env python3
"""Sequential real-runtime acceptance; defaults to DeepAgent and native Codex."""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import subprocess
from pathlib import Path

from dotenv import dotenv_values

from examples._common import agent_class
from netopsbench.platform.runtime.toolchain import runtime_tools
from netopsbench.platform.scenario.parser import parse_scenario_file, save_scenario_file
from netopsbench.platform.session.acceptance import run_acceptance
from netopsbench.platform.topology.config import DEFAULT_CLIENT_IMAGE
from netopsbench.sdk import NetOpsBench


def select_cases(paths: list[Path], include_faults: bool, manifest) -> list[Path]:
    healthy, faults = [], {}
    for path in paths:
        scenario = parse_scenario_file(path)
        if scenario.episode.is_healthy:
            healthy.append(path)
        elif scenario.episode.fault_type != "none":
            faults.setdefault(scenario.episode.fault_type, []).append(path)
    if not healthy or (include_faults and len(faults) != 12):
        raise RuntimeError("Acceptance campaign must contain healthy samples and twelve fault types")
    selected = [sorted(healthy)[0]]
    if include_faults:
        scenarios = {path: parse_scenario_file(path) for paths in faults.values() for path in paths}
        chosen = {}
        role_counts, pod_counts = {}, {}

        def device(path):
            return manifest.device(scenarios[path].episode.target_device)

        def number(path):
            return int(re.findall(r"\d+", device(path).name)[-1])

        def choose(kind, path):
            chosen[kind] = path
            target = device(path)
            role_counts[target.role] = role_counts.get(target.role, 0) + 1
            pod = target.metadata.get("pod")
            pod_counts[pod] = pod_counts.get(pod, 0) + 1

        # Exercise the address-allocation boundary that previously capped racks.
        high_edges = [path for path in scenarios if device(path).role.value == "edge" and number(path) > 155]
        if manifest.facts.num_edges > 155:
            if not high_edges:
                raise RuntimeError("Acceptance campaign lacks an edge above the old 155-rack boundary")
            path = max(high_edges, key=number)
            choose(scenarios[path].episode.fault_type, path)
        kinds = sorted(faults, key=lambda kind: (len({device(path).role for path in faults[kind]}), kind))
        priority = {"core": 0, "agg": 1, "edge": 2}
        for kind in kinds:
            if kind in chosen:
                continue
            roles = {device(path).role for path in faults[kind]}
            role = min(roles, key=lambda role: (role_counts.get(role, 0), priority[role.value]))
            candidates = [path for path in faults[kind] if device(path).role == role]
            path = max(
                candidates, key=lambda path: (-pod_counts.get(device(path).metadata.get("pod"), 0), number(path))
            )
            choose(kind, path)
        if set(role.value for role in role_counts) != {"core", "agg", "edge"}:
            raise RuntimeError("Acceptance fault selection must exercise all three switch tiers")
        selected.extend(chosen[kind] for kind in sorted(chosen))
    return selected


def smoke_cases(paths, manifest):
    selected = {}
    for path in sorted(paths):
        kind = parse_scenario_file(path).episode.fault_type
        if kind in {"none", "link_down", "device_down"}:
            selected.setdefault(kind, path)
    if len(selected) != 3:
        raise RuntimeError("XS smoke requires healthy, link_down and device_down cases")
    # Keep every source client able to send. Access-link/leaf outages can
    # generate genuine local send errors, which are not complete observations.
    switches = {device.name for device in manifest.switches()}
    fabric = next(link for link in manifest.links if all(endpoint.device in switches for endpoint in link.endpoints))
    leaf = next(endpoint for endpoint in fabric.endpoints if manifest.device(endpoint.device).role.value == "leaf")
    spine = next(device for device in manifest.switches() if device.role.value == "spine")
    directory = paths[0].parent / "codex-smoke"
    cases = [selected["none"]]
    for kind, device, interface in (("link_down", leaf.device, leaf.interface), ("device_down", spine.name, None)):
        source = parse_scenario_file(selected[kind])
        case = source.model_copy(
            update={
                "scenario_id": f"codex_smoke_{kind}_xs",
                "episode": source.episode.model_copy(
                    update={
                        "target_device": device,
                        "target_interface": interface,
                        "description": "XS fabric lifecycle smoke",
                    }
                ),
            }
        )
        path = directory / f"{case.id}.yaml"
        if not path.exists():
            save_scenario_file(case, path)
        cases.append(path)
    return cases


def preflight_agents(backends, vendor, output, *, expected=None):
    """Validate every configuration before making small real model requests."""
    expected = expected or {}
    identities, agents = {}, {}
    runtime_tools()
    image = os.environ.get("NETOPSBENCH_CLIENT_IMAGE", DEFAULT_CLIENT_IMAGE)
    subprocess.check_output(["docker", "image", "inspect", image, "--format", "{{.Id}}"], text=True)
    for backend in backends:
        if backend == "deepagent":
            agent = agent_class(backend)(vendor=vendor)
            env = agent._provider.PRESET["api_key_env"]
            key = os.environ.get(env) or dotenv_values(Path.cwd() / ".env").get(env)
            if not key:
                raise RuntimeError(f"{env} must be configured before deployment")
            os.environ[env] = key
            agent.api_key = key
            identities[backend] = {
                "vendor": vendor,
                "model": agent.model,
                "base_url": agent.base_url,
                "max_graph_tool_steps": agent.max_tool_calls,
                "request_timeout_seconds": agent.timeout_seconds,
            }
        else:
            agent = agent_class(backend)(artifacts_dir=output / "codex-preflight")
            if backend in expected:
                current = {
                    key: getattr(agent, key) for key in ("cli_version", "model", "reasoning_effort", "timeout_seconds")
                }
                current["max_mcp_calls"] = agent.max_tool_calls
                current["image_id"] = subprocess.check_output(
                    ["docker", "image", "inspect", agent.image, "--format", "{{.Id}}"], text=True
                ).strip()
                if current != expected[backend]:
                    raise ValueError(f"Cannot change {backend} configuration when resuming acceptance")
                identities[backend] = current
        agents[backend] = agent
        if backend in expected and expected[backend] != identities[backend]:
            raise ValueError(f"Cannot change {backend} configuration when resuming acceptance")
    for backend, agent in agents.items():
        if backend == "deepagent":
            llm = agent._provider.build_llm(
                model=agent.model,
                api_key=agent.api_key,
                base_url=agent.base_url,
                temperature=agent.temperature,
                max_tokens=32,
                timeout_seconds=agent.timeout_seconds,
            )
            llm.invoke("Reply with OK only.")
        else:
            # Every resumed configuration was checked before auth writes or image execution.
            identities[backend] = agent.preflight()
            if backend in expected and identities[backend] != expected[backend]:
                raise ValueError(f"Cannot change {backend} configuration when resuming acceptance")
            asyncio.run(agent.check_connection())
    return identities


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scales", default="xlarge,fat-tree-k18")
    parser.add_argument("--vendor", default="deepseek")
    parser.add_argument("--agents", nargs="+", choices=["deepagent", "codex"], default=["deepagent", "codex"])
    parser.add_argument("--output", type=Path, default=Path(".netopsbench/validation-pingmesh-v2"))
    parser.add_argument("--capacity-sample", type=Path)
    parser.add_argument("--allow-tight-capacity", action="store_true")
    parser.add_argument("--resume-runtime", type=Path)
    parser.add_argument("--rebuild-runtime", action="store_true")
    args = parser.parse_args()
    return run_acceptance(
        args,
        bench_factory=NetOpsBench,
        agent_factory=agent_class,
        preflight=preflight_agents,
        select_cases=select_cases,
        smoke_cases=smoke_cases,
    )


if __name__ == "__main__":
    raise SystemExit(main())
