from netopsbench.platform.worker import health


def test_active_interface_coverage_flags_xlarge_spine_with_only_32_ports_up():
    topo = {
        "devices": {
            "spines": [{"name": f"spine{i}"} for i in range(1, 17)],
            "leafs": [{"name": f"leaf{i}"} for i in range(1, 129)],
            "clients": [{"name": f"client{i}", "leaf": f"leaf{i}"} for i in range(1, 129)],
        },
        "scale": {"num_spines": 16, "num_leafs": 128, "clients_per_leaf": 1},
    }
    output = "\n".join(
        f"Ethernet{idx * 4} 1,2,3,4 100G 9100 N/A up up QSFP" for idx in range(32)
    )

    active = health._parse_active_interfaces(output)
    assert len(active) == 32
    assert health._expected_active_interface_count(topo, "spine1") == 128
    assert health._expected_active_interface_count(topo, "leaf128") == 17

    error = health._active_interface_coverage_error(
        container="clab-xlarge-spine1",
        device="spine1",
        active_interfaces=active,
        expected_count=128,
    )
    assert error == "active interface coverage too low on clab-xlarge-spine1: active=32 expected>=128"


def test_active_interface_coverage_accepts_required_count():
    output = "\n".join(
        f"Ethernet{idx * 4} 1,2,3,4 100G 9100 N/A up up QSFP" for idx in range(128)
    )

    error = health._active_interface_coverage_error(
        container="clab-xlarge-spine1",
        device="spine1",
        active_interfaces=health._parse_active_interfaces(output),
        expected_count=128,
    )
    assert error is None
