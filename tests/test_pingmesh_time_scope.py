import json

from netopsbench.platform.toolkit._core.common import ToolResult
from netopsbench.platform.toolkit.mcp import context as mcp_context
from netopsbench.platform.toolkit.mcp import observability as fastmcp_server
from netopsbench.platform.toolkit.toolkit import AgentToolkit


def _toolkit_with_captured_queries(monkeypatch):
    toolkit = AgentToolkit(topology_metadata={"devices": {}})
    captured = {}

    def fake_query(query, require_value=True):
        captured["query"] = query
        captured["require_value"] = require_value
        return []

    monkeypatch.setattr(toolkit, "_query_influx_rows", fake_query)
    return toolkit, captured


def test_pingmesh_time_scope_uses_explicit_window_first(monkeypatch):
    toolkit, captured = _toolkit_with_captured_queries(monkeypatch)
    toolkit.set_pingmesh_time_window("2026-01-01T00:00:00Z", "2026-01-01T00:01:00Z")

    result = toolkit.get_pingmesh_summary(
        time_range_minutes=10,
        start_time="2026-01-02T00:00:00Z",
        end_time="2026-01-02T00:01:00Z",
    )

    assert result.success is True
    assert result.data["time_scope"]["source"] == "explicit"
    assert 'range(start: time(v: "2026-01-02T00:00:00Z")' in captured["query"]


def test_pingmesh_time_scope_uses_toolkit_default_before_context_file(monkeypatch, tmp_path):
    context_file = tmp_path / "pingmesh-window.json"
    context_file.write_text(
        json.dumps({"start_time": "2026-01-03T00:00:00Z", "end_time": "2026-01-03T00:01:00Z"}),
        encoding="utf-8",
    )
    monkeypatch.setenv("NETOPSBENCH_PINGMESH_CONTEXT_FILE", str(context_file))
    toolkit, captured = _toolkit_with_captured_queries(monkeypatch)
    toolkit.set_pingmesh_time_window("2026-01-02T00:00:00Z", "2026-01-02T00:01:00Z")

    result = toolkit.get_pingmesh_hotspots()

    assert result.success is True
    assert result.data["time_scope"]["source"] == "toolkit_default"
    assert 'range(start: time(v: "2026-01-02T00:00:00Z")' in captured["query"]


def test_pingmesh_time_scope_uses_context_file_before_env(monkeypatch, tmp_path):
    context_file = tmp_path / "pingmesh-window.json"
    context_file.write_text(
        json.dumps({"start_time": "2026-01-03T00:00:00Z", "end_time": "2026-01-03T00:01:00Z"}),
        encoding="utf-8",
    )
    monkeypatch.setenv("NETOPSBENCH_PINGMESH_CONTEXT_FILE", str(context_file))
    monkeypatch.setenv("NETOPSBENCH_PINGMESH_START_TIME", "2026-01-04T00:00:00Z")
    monkeypatch.setenv("NETOPSBENCH_PINGMESH_END_TIME", "2026-01-04T00:01:00Z")
    toolkit, captured = _toolkit_with_captured_queries(monkeypatch)

    result = toolkit.get_pingmesh_summary()

    assert result.success is True
    assert result.data["time_scope"]["source"] == "context_file"
    assert 'range(start: time(v: "2026-01-03T00:00:00Z")' in captured["query"]


def test_pingmesh_time_scope_uses_env_before_rolling(monkeypatch):
    monkeypatch.setenv("NETOPSBENCH_PINGMESH_START_TIME", "2026-01-04T00:00:00Z")
    monkeypatch.setenv("NETOPSBENCH_PINGMESH_END_TIME", "2026-01-04T00:01:00Z")
    toolkit, captured = _toolkit_with_captured_queries(monkeypatch)

    result = toolkit.get_pingmesh_summary()

    assert result.success is True
    assert result.data["time_scope"]["source"] == "env"
    assert 'range(start: time(v: "2026-01-04T00:00:00Z")' in captured["query"]


def test_pingmesh_time_scope_falls_back_to_rolling(monkeypatch):
    toolkit, captured = _toolkit_with_captured_queries(monkeypatch)

    result = toolkit.get_pingmesh_summary(time_range_minutes=7)

    assert result.success is True
    assert result.data["time_scope"] == {
        "mode": "rolling",
        "source": "time_range_minutes",
        "time_range_minutes": 7,
    }
    assert "|> range(start: -7m)" in captured["query"]


def test_fastmcp_pingmesh_tools_pass_absolute_window(monkeypatch):
    calls = {}

    class FakeToolkit:
        def get_pingmesh_summary(self, **kwargs):
            calls["summary"] = kwargs
            return ToolResult(success=True, data={"ok": "summary"})

        def get_pingmesh_hotspots(self, **kwargs):
            calls["hotspots"] = kwargs
            return ToolResult(success=True, data={"ok": "hotspots"})

    monkeypatch.setattr(mcp_context, "_toolkit", FakeToolkit())

    assert fastmcp_server.get_pingmesh_summary(
        start_time="2026-01-01T00:00:00Z",
        end_time="2026-01-01T00:01:00Z",
    ) == {"ok": "summary"}
    assert fastmcp_server.get_pingmesh_hotspots(
        limit=3,
        start_time="2026-01-01T00:00:00Z",
        end_time="2026-01-01T00:01:00Z",
    ) == {"ok": "hotspots"}
    assert calls["summary"]["start_time"] == "2026-01-01T00:00:00Z"
    assert calls["summary"]["end_time"] == "2026-01-01T00:01:00Z"
    assert calls["hotspots"]["limit"] == 3
    assert calls["hotspots"]["start_time"] == "2026-01-01T00:00:00Z"


def test_builtin_mcp_config_passes_netopsbench_env(monkeypatch):
    from netopsbench.sdk.mcp import builtin_mcp_server_config

    monkeypatch.setenv("NETOPSBENCH_PINGMESH_CONTEXT_FILE", "/tmp/window.json")
    config = builtin_mcp_server_config()

    assert config["netopsbench"]["env"]["NETOPSBENCH_PINGMESH_CONTEXT_FILE"] == "/tmp/window.json"
