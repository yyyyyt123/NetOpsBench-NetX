"""Regression tests for structured active fault tracking."""

import tempfile

from netopsbench.platform.faults.injector import FaultInjector
from netopsbench.platform.faults.models import ActiveFault
from netopsbench.platform.topology.generator import generate_topology


def _metadata() -> dict:
    with tempfile.TemporaryDirectory() as tmpdir:
        return generate_topology("xs", tmpdir)["metadata"]


def test_active_fault_dataclass_roundtrips_metadata_to_dict():
    fault = ActiveFault(
        type="packet_loss",
        device="leaf1",
        interface="eth1",
        metadata={"loss_pct": 10, "linux_interface": "eth1"},
    )

    payload = fault.to_dict()

    assert payload["type"] == "packet_loss"
    assert payload["device"] == "leaf1"
    assert payload["interface"] == "eth1"
    assert payload["loss_pct"] == 10
    assert payload["linux_interface"] == "eth1"


def test_active_fault_tracker_preserves_domain_objects():
    injector = FaultInjector(topology_metadata=_metadata())
    injector.active_faults = [
        ActiveFault(
            type="static_route_misconfig",
            device="leaf1",
            metadata={"target_ip": "192.168.1.2/32", "wrong_nexthop": "192.168.1.1"},
        )
    ]

    assert len(injector.active_faults) == 1
    assert isinstance(injector.active_faults[0], ActiveFault)
    assert injector.active_faults[0].metadata["target_ip"] == "192.168.1.2/32"


def test_link_flapping_uses_python_background_control_metadata():
    injector = FaultInjector(topology_metadata=_metadata())

    stub = type("R", (), {"returncode": 0, "stderr": "", "stdout": ""})()
    injector._sonic.config_cmd = lambda *args, **kwargs: stub
    injector._cmd.docker_exec = lambda *args, **kwargs: stub

    fault = injector.inject_link_flapping(device="spine1", interface="Ethernet0", iterations=1, down_time=0, up_time=0)

    assert fault["type"] == "link_flapping"
    assert fault["orchestration"] == "python"
    assert "task_id" in fault
    assert "pid" not in fault
