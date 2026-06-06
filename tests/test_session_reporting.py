from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

from netopsbench.platform.session.reporting import create_run_report, next_run_id


def test_next_run_id_uses_utc_timestamp_and_collision_suffix(tmp_path: Path):
    artifact_root = tmp_path / "runs"
    started_at = datetime(2026, 6, 5, 12, 40, 40, tzinfo=UTC)

    assert next_run_id(artifact_root, started_at=started_at) == "run-20260605T124040Z"

    (artifact_root / "run-20260605T124040Z").mkdir(parents=True)
    assert next_run_id(artifact_root, started_at=started_at) == "run-20260605T124040Z-02"

    (artifact_root / "run-20260605T124040Z-02").mkdir()
    assert next_run_id(artifact_root, started_at=started_at) == "run-20260605T124040Z-03"


def test_create_run_report_preserves_topology_scale_and_agent_name(tmp_path: Path):
    runtime = SimpleNamespace(id="run-0001-runtime", scale="small")
    agent = SimpleNamespace(name="agent-x")
    scenario = SimpleNamespace(id="generated_link_down_small_001", scale="small")

    report = create_run_report(
        run_id="run-0001",
        mode="suite",
        started_at=datetime(2026, 4, 29, 1, 0, tzinfo=UTC),
        completed_at=datetime(2026, 4, 29, 2, 0, tzinfo=UTC),
        runtime=runtime,
        runtime_owner="sdk",
        teardown="always",
        scenarios=[scenario],
        agent=agent,
        worker_summaries=[{"success": True}],
        scenario_summaries=[],
        aggregate_report={
            "agent_name": "agent-x",
            "topology_scale": "small",
            "summary": {"total_cases": 1, "overall_accuracy": 1.0},
            "detailed_results": [],
        },
        artifact_dir=tmp_path,
        raw_dir=tmp_path / "raw",
        traces_dir=tmp_path / "traces",
        trace_index_path=tmp_path / "traces" / "index.jsonl",
        report_path=tmp_path / "report.json",
        metadata_path=tmp_path / "metadata.json",
    )

    assert report["agent_name"] == "agent-x"
    assert report["topology_scale"] == "small"
    assert report["summary"]["agent_name"] == "agent-x"
    assert report["summary"]["topology_scale"] == "small"
    assert report["raw"]["topology_scale"] == "small"
    assert report["artifact_paths"]["traces_dir"] == str(tmp_path / "traces")
    assert report["artifact_paths"]["trace_index"] == str(tmp_path / "traces" / "index.jsonl")
