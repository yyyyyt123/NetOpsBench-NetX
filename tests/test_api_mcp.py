"""Tests for the public SDK MCP helpers."""

from __future__ import annotations

from pathlib import Path

from netopsbench.sdk.mcp import builtin_mcp_server_command, builtin_mcp_server_config, start_builtin_mcp_server


def test_builtin_mcp_server_config_has_expected_shape(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir(parents=True, exist_ok=True)

    config = builtin_mcp_server_config(workspace=repo)

    assert "netopsbench" in config
    server = config["netopsbench"]
    assert server["transport"] == "stdio"
    assert server["args"] == ["-m", "netopsbench.platform.toolkit.fastmcp_server"]
    assert Path(server["cwd"]) == repo.resolve()
    assert "PYTHONPATH" not in server["env"]


def test_builtin_mcp_server_config_only_forwards_tool_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("NETOPSBENCH_TOPOLOGY_DIR", "/tmp/topology")
    monkeypatch.setenv("NETOPSBENCH_INFLUXDB_BUCKET", "worker-bucket")
    monkeypatch.setenv("NETOPSBENCH_WORKER_DEPLOY_JOBS", "99")
    monkeypatch.setenv("OPENAI_API_KEY", "secret")

    config = builtin_mcp_server_config(
        workspace=tmp_path,
        env={
            "NETOPSBENCH_PINGMESH_CONTEXT_FILE": "/tmp/window.json",
            "DEEPSEEK_API_KEY": "secret",
        },
    )["netopsbench"]["env"]

    assert config == {
        "NETOPSBENCH_INFLUXDB_BUCKET": "worker-bucket",
        "NETOPSBENCH_PINGMESH_CONTEXT_FILE": "/tmp/window.json",
        "NETOPSBENCH_TOPOLOGY_DIR": "/tmp/topology",
    }


def test_builtin_mcp_server_command_uses_python_and_script(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir(parents=True, exist_ok=True)

    command = builtin_mcp_server_command(workspace=repo)

    assert command["args"] == ["-m", "netopsbench.platform.toolkit.fastmcp_server"]
    assert command["cwd"] == str(repo.resolve())
    assert command["command"]


def test_start_builtin_mcp_server_returns_stoppable_handle(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir(parents=True, exist_ok=True)

    calls = {}

    class _FakePopen:
        def __init__(self, args, cwd=None, env=None, stdin=None, stdout=None, stderr=None):
            calls["args"] = args
            calls["cwd"] = cwd
            calls["env"] = env
            self.pid = 12345
            self._returncode = None

        def poll(self):
            return self._returncode

        def terminate(self):
            self._returncode = 0

        def wait(self, timeout=None):
            return self._returncode

        def kill(self):
            self._returncode = -9

    monkeypatch.setenv("OPENAI_API_KEY", "must-not-reach-tools")
    monkeypatch.setattr("netopsbench.sdk.mcp.subprocess.Popen", _FakePopen)

    handle = start_builtin_mcp_server(workspace=repo)

    assert handle.pid == 12345
    assert calls["args"][1:] == ["-m", "netopsbench.platform.toolkit.fastmcp_server"]
    assert calls["cwd"] == str(repo.resolve())
    assert "OPENAI_API_KEY" not in calls["env"]

    handle.stop()
    assert handle.poll() == 0
