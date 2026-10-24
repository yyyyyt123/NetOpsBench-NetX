"""Bounded, phase-scoped route snapshots and uniformly derived public facts."""

from __future__ import annotations

import ipaddress
import json
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

from netopsbench.platform.observability.bgp_parser import parse_bgp_summary
from netopsbench.platform.topology.topology_utils import clab_container_name
from netopsbench.platform.utils.control import OperationControl, current_operation_control
from netopsbench.platform.utils.proc import docker_prefix, safe_run

_NETWORK_PROBE_SOURCE = Path(__file__).with_name("_routing_probe.py").read_text(encoding="utf-8")


def next_hops(route):
    return route.get("nexthops") or (
        [{"gateway": route.get("gateway"), "dev": route.get("dev")}] if route.get("gateway") or route.get("dev") else []
    )


def verify_route_recovery(baseline, recovery):
    """Require complete observed BGP forwarding, using an existing node snapshot."""
    if baseline is None:
        return []
    errors = []
    for device, before in baseline["nodes"].items():
        after = recovery.get("nodes", {}).get(device, {})
        if after.get("error") or "fib" not in after:
            errors.append(f"{device}: missing recovery FIB")
            continue
        old = {r.get("dst"): r for r in before.get("fib", []) if r.get("protocol") == "bgp"}
        current = {r.get("dst"): r for r in after["fib"]}
        before_static = {
            (
                r.get("dst"),
                r.get("protocol"),
                r.get("type", "unicast"),
                tuple(sorted((h.get("gateway") or "", h.get("dev") or "") for h in next_hops(r))),
            )
            for r in before.get("fib", [])
            if r.get("protocol") == "static" or r.get("type") in {"blackhole", "unreachable", "prohibit"}
        }
        for route in after["fib"]:
            if route.get("protocol") == "static" or route.get("type") in {"blackhole", "unreachable", "prohibit"}:
                identity = (
                    route.get("dst"),
                    route.get("protocol"),
                    route.get("type", "unicast"),
                    tuple(sorted((h.get("gateway") or "", h.get("dev") or "") for h in next_hops(route))),
                )
                if identity not in before_static and route.get("dst") not in old:
                    errors.append(f"{device}/{route.get('dst')}: unexpected residual static/drop route")
        for prefix, route in old.items():
            restored = current.get(prefix)
            expected_hops = {(hop.get("gateway"), hop.get("dev")) for hop in next_hops(route)}
            restored_hops = {(hop.get("gateway"), hop.get("dev")) for hop in next_hops(restored or {})}
            if not restored or restored.get("protocol") != "bgp" or restored_hops != expected_hops:
                errors.append(f"{device}/{prefix}: forwarding not restored")
    return errors


def collect_routes(manifest, *, cancel_event=None, deadline=None):
    inherited = current_operation_control()
    if inherited is not None and inherited.deadline is not None:
        deadline = min(deadline, inherited.deadline) if deadline is not None else inherited.deadline
    control = OperationControl(cancel_event, deadline if deadline is not None else time.monotonic() + 60)
    nodes = {}

    def probe(device):
        control.remaining()
        container = clab_container_name(manifest.name, device.name)
        result = safe_run(
            [
                *docker_prefix(),
                "docker",
                "exec",
                container,
                "python3",
                "-c",
                _NETWORK_PROBE_SOURCE,
                "--routes-only",
                "--budget-seconds",
                str(control.remaining(60)),
            ],
            timeout=control.remaining(60),
            control=control,
        )
        if result.returncode:
            return device.name, {
                "error": result.stderr or "Node snapshot failed",
                "collected_at": datetime.now(UTC).isoformat(),
            }
        payload = json.loads(result.stdout)
        checks = payload["checks"]
        fib = checks["fib"]
        if fib["returncode"]:
            return device.name, {"error": "FIB collection failed", "collected_at": datetime.now(UTC).isoformat()}
        bgp_ok = checks["bgp"]["returncode"] == 0
        try:
            bgp = parse_bgp_summary(checks["bgp"]["stdout"], strict=True)
        except ValueError:
            bgp, bgp_ok = [], False
        return device.name, {
            "packet_counters": payload.get("packet_counters", {"error": "Not collected"}),
            "fib": json.loads(fib["stdout"]),
            "bgp": bgp,
            "bgp_collection_ok": bgp_ok,
            "interfaces": checks.get("interfaces", {}).get("stdout", ""),
            "collected_at": datetime.now(UTC).isoformat(),
        }

    pool = ThreadPoolExecutor(max_workers=min(16, len(manifest.switches())))
    futures = []
    try:
        for device in manifest.switches():
            control.remaining()
            futures.append(pool.submit(probe, device))
        for future in futures:
            name, row = future.result(timeout=control.remaining())
            nodes[name] = row
    finally:
        pool.shutdown(wait=True, cancel_futures=True)
    return {"nodes": nodes, "collected_at": datetime.now(UTC).isoformat()}


def route_facts(baseline, current, manifest):
    """No episode, injection receipt or answer participates in public discovery."""
    networks = {str(ipaddress.ip_network(f"{client.data_ip}/30", strict=False)) for client in manifest.clients()}
    client_addresses = {
        int(ipaddress.ip_address(client.data_ip)): str(ipaddress.ip_network(f"{client.data_ip}/30", strict=False))
        for client in manifest.clients()
    }
    changes, collection_errors = [], []
    for name in sorted(set(baseline["nodes"]) | set(current["nodes"])):
        before, after = baseline["nodes"].get(name, {}), current["nodes"].get(name, {})
        if before.get("error") or after.get("error"):
            collection_errors.append({"device": name, "error": after.get("error") or before.get("error")})
            continue

        def routes(row):
            return {r.get("dst"): r for r in row.get("fib", []) if r.get("dst") in networks}

        old, new = routes(before), routes(after)
        for prefix in sorted(set(old) | set(new)):
            old_count, new_count = (
                len(next_hops(old[prefix])) if prefix in old else 0,
                len(next_hops(new[prefix])) if prefix in new else 0,
            )
            if old_count and new_count < old_count:
                changes.append(
                    {
                        "device": name,
                        "prefix": prefix,
                        "event": "prefix_missing" if not new_count else "next_hops_reduced",
                        "next_hops_before": old_count,
                        "next_hops_after": new_count,
                    }
                )
        # Host routes can override a still-present healthy /30. Report these
        # actual FIB changes uniformly, including cases with normal Pingmesh.
        previous = {r.get("dst"): r for r in before.get("fib", [])}
        for route in after.get("fib", []):
            dst = route.get("dst")
            if dst in {None, "default"}:
                continue
            network = ipaddress.ip_network(dst, strict=False)
            if network.prefixlen <= 30:
                continue
            for address in network:
                prefix = client_addresses.get(int(address))
                if prefix is None:
                    continue
                earlier = previous.get(dst, old.get(prefix, {}))
                if previous.get(dst) is not None and (
                    earlier.get("type", "unicast") == route.get("type", "unicast")
                    and {(h.get("gateway"), h.get("dev")) for h in next_hops(earlier)}
                    == {(h.get("gateway"), h.get("dev")) for h in next_hops(route)}
                ):
                    continue
                changes.append(
                    {
                        "device": name,
                        "prefix": prefix,
                        "event": "more_specific_route_changed",
                        "matched_prefix": dst,
                        "route_type": route.get("type", "unicast"),
                        "next_hops_before": len(next_hops(earlier)),
                        "next_hops_after": len(next_hops(route)),
                    }
                )
        old_peers = {row["neighbor"]: row for row in before.get("bgp", [])}
        for phase, snapshot in (("baseline", before), ("current", after)):
            if snapshot.get("bgp_collection_ok") is False:
                collection_errors.append(
                    {"device": name, "source": "bgp", "phase": phase, "error": "BGP table collection failed"}
                )
        if before.get("bgp_collection_ok") is False or after.get("bgp_collection_ok") is False:
            continue
        for row in after.get("bgp", []):
            old_row = old_peers.get(row["neighbor"], {})
            old_count, new_count = old_row.get("prefixes_received"), row.get("prefixes_received")
            if old_count is not None and new_count is not None and old_count != new_count:
                changes.append(
                    {
                        "device": name,
                        "peer": row["neighbor"],
                        "event": "prefix_count_changed",
                        "prefixes_before": old_count,
                        "prefixes_after": new_count,
                    }
                )
    return {"changes": changes, "collection_errors": collection_errors, "collected_at": current["collected_at"]}
