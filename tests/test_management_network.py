import unittest

import dns.message
import dns.rcode
import dns.rdatatype
import pytest

from netopsbench.platform.runtime.management import DNSRegistry, dns_answer, state_dir


@pytest.mark.parametrize("protocol", ["udp", "tcp"])
def test_dns_concurrency_is_bounded_and_slots_are_released(protocol):
    import socket
    import socketserver
    import threading

    from netopsbench.platform.runtime.management import TCP, UDP

    release = threading.Event()
    saturated = threading.Event()
    rejected = threading.Event()
    calls = []

    class Handler(socketserver.BaseRequestHandler):
        def handle(self):
            calls.append(self.client_address)
            if len(calls) == 2:
                saturated.set()
            release.wait(3)

    class Server(UDP if protocol == "udp" else TCP):
        max_requests = 2

        def shutdown_request(self, request):
            if not release.is_set():
                rejected.set()
            super().shutdown_request(request)

    with Server(("127.0.0.1", 0), Handler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        sockets = []
        try:
            for _ in range(2):
                client = socket.socket(type=socket.SOCK_DGRAM if protocol == "udp" else socket.SOCK_STREAM)
                sockets.append(client)
                client.connect(server.server_address)
                client.send(b"request")
            assert saturated.wait(2)
            third = socket.socket(type=socket.SOCK_DGRAM if protocol == "udp" else socket.SOCK_STREAM)
            sockets.append(third)
            third.connect(server.server_address)
            third.send(b"request")
            assert rejected.wait(2)
            assert len(calls) == 2
            release.set()
            assert server.request_slots.acquire(timeout=2)
            assert server.request_slots.acquire(timeout=2)
            server.request_slots.release()
            server.request_slots.release()
        finally:
            release.set()
            for client in sockets:
                client.close()
            server.shutdown()
            thread.join(timeout=2)


class Tests(unittest.TestCase):
    def test_dns(self):
        records = {"client1": "172.31.224.11", "clab-test-client1": "172.31.224.11"}
        for typ in ["A", "AAAA", "PTR"]:
            name = "11.224.31.172.in-addr.arpa" if typ == "PTR" else "client1"
            q = dns.message.make_query(name, typ)
            a = dns.message.from_wire(dns_answer(q.to_wire(), records, []))
            self.assertEqual(a.id, q.id)
            self.assertEqual(a.rcode(), 0)
            self.assertEqual(bool(a.answer), typ != "AAAA")
        q = dns.message.make_query("unresolved.invalid", "A")
        self.assertEqual(dns.message.from_wire(dns_answer(q.to_wire(), {}, [])).rcode(), dns.rcode.SERVFAIL)

    def test_docker_dns_lifecycle(self):
        current = {"Containers": {"id": {"Name": "influxdb", "IPv4Address": "172.31.224.2/23", "EndpointID": "ep1"}}}

        def request(method, path):
            if path.startswith("/networks"):
                return current
            return {"NetworkSettings": {"Networks": {"lab": {"Aliases": ["metrics"], "DNSNames": ["influxdb"]}}}}

        registry = DNSRegistry({"network": "lab", "records": {"client1": "172.31.224.11"}}, request=request)
        self.assertEqual(registry.snapshot()["metrics"], "172.31.224.2")
        current["Containers"] = {}
        self.assertNotIn("metrics", registry.snapshot(force=True))
        self.assertEqual(registry.snapshot()["client1"], "172.31.224.11")

    def test_scope(self):
        for name in ["", "../test", "mgmt-a/b"]:
            with self.assertRaises(ValueError):
                state_dir(name)


if __name__ == "__main__":
    unittest.main()


def test_prepare_supports_default_names_and_preserves_management_metadata(tmp_path, monkeypatch):
    import json

    import yaml

    from netopsbench.platform.runtime import management as m

    monkeypatch.setattr(m, "STATE_ROOT", tmp_path / "state")
    calls = []
    monkeypatch.setattr(m, "api", lambda method, path, data=None: calls.append((method, path, data)))
    monkeypatch.setattr(m, "start_daemon", lambda d: None)
    path = tmp_path / "bench-xs.clab.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "name": "bench-xs",
                "mgmt": {"network": "clab-bench", "ipv4-subnet": "172.31.1.0/24"},
                "topology": {"nodes": {"client1": {"kind": "linux", "mgmt-ipv4": "172.31.1.10"}}},
            }
        )
    )
    d = m.prepare(path)
    manifest = json.loads((d / "manifest.json").read_text())
    node = yaml.safe_load(path.read_text())["topology"]["nodes"]["client1"]
    assert node["dns"]["servers"] == ["172.31.1.1"]
    assert json.loads(node["labels"][m.LABEL])["IPv4addr"] == "172.31.1.10"
    assert manifest["records"]["client1.bench-xs.lab"] == "172.31.1.10"
    created = next(data for method, path, data in calls if method == "POST")
    assert created["IPAM"]["Config"][0]["AuxiliaryAddresses"] == {"client1": "172.31.1.10"}
    assert created["Labels"][m.OWNER] == "bench-xs"


def test_cleanup_refuses_foreign_network_before_deleting_interfaces(tmp_path, monkeypatch):
    import json

    import pytest

    from netopsbench.platform.runtime import management as m

    (tmp_path / "manifest.json").write_text(json.dumps({"lab": "ours", "network": "foreign", "nodes": {"client": {}}}))
    calls = []
    monkeypatch.setattr(m, "api", lambda *args: {"Labels": {m.OWNER: "another-lab"}})
    monkeypatch.setattr(m, "helper", lambda *args, **kwargs: calls.append("deleted"))
    with pytest.raises(RuntimeError, match="ownership"):
        m.cleanup_network(tmp_path)
    assert calls == []


def test_stopped_controller_pid_reuse_never_signals_another_process(tmp_path, monkeypatch):
    import json

    from netopsbench.platform.runtime import management as m

    (tmp_path / "daemon.json").write_text(json.dumps({"pid": 123, "birth": "old"}))
    signals = []
    monkeypatch.setattr(m, "birth", lambda pid: "new")
    monkeypatch.setattr(m.os, "kill", lambda *args: signals.append(args))
    m.stop_daemon(tmp_path)
    assert not signals


def test_toolchain_rejects_missing_or_wrong_build(tmp_path, monkeypatch):
    import json

    import pytest

    from netopsbench.platform.runtime import toolchain as t

    monkeypatch.setenv("NETOPSBENCH_RUNTIME_TOOLS", str(tmp_path))
    with pytest.raises(RuntimeError, match="Build the pinned"):
        t.runtime_tools()
    (tmp_path / "manifest.json").write_text(json.dumps({"commit": "wrong"}))
    with pytest.raises(RuntimeError, match="does not match"):
        t.runtime_tools()


def test_toolchain_requires_both_fast_network_settings(tmp_path, monkeypatch):
    import json

    import pytest

    from netopsbench.platform.runtime import toolchain as t

    monkeypatch.setenv("NETOPSBENCH_RUNTIME_TOOLS", str(tmp_path))
    manifest = json.loads((t.ASSETS / "containerlab.lock.json").read_text())
    for name in ("containerlab", "mgmt-link"):
        binary = tmp_path / name
        binary.touch()
        binary.chmod(0o755)
    for mode, deferred in [("off", True), ("both", False)]:
        manifest.update(direct_veth_mode=mode, sonic_deferred_endpoints=deferred)
        (tmp_path / "manifest.json").write_text(json.dumps(manifest))
        with pytest.raises(RuntimeError, match="must"):
            t.runtime_tools()
    manifest.update(direct_veth_mode="both", sonic_deferred_endpoints=True)
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    assert t.runtime_tools() == tmp_path
