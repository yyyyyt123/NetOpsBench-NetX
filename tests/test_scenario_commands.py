"""Scenario command coverage for query-focused CLI."""

import pytest

from netopsbench.cli import build_parser


def test_scenario_cli_exposes_only_list_and_validate(capsys):
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["scenario", "--help"])
    output = capsys.readouterr().out
    assert "list" in output
    assert "validate" in output
    assert "generate" in output
    assert "run" not in output


def test_scenario_cli_rejects_legacy_run_command():
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["scenario", "run", "scenarios/xs"])


def test_cli_accepts_xlarge_and_fat_tree_generation_commands():
    parser = build_parser()

    topology_args = parser.parse_args(["topology", "generate", "--scale", "xlarge"])
    scenario_args = parser.parse_args(["scenario", "generate", "--scale", "xlarge"])
    benchmark_args = parser.parse_args(["benchmark", "prepare", "--scales", "xlarge"])
    fat_tree_topology_args = parser.parse_args(["topology", "generate", "--scale", "fat-tree-k12"])
    fat_tree_scenario_args = parser.parse_args(["scenario", "generate", "--scale", "fat-tree-k8"])
    fat_tree_benchmark_args = parser.parse_args(["benchmark", "prepare", "--scales", "fat-tree-k8,fat-tree-k12"])

    assert topology_args.scale == "xlarge"
    assert scenario_args.scale == "xlarge"
    assert benchmark_args.scales == "xlarge"
    assert fat_tree_topology_args.scale == "fat-tree-k12"
    assert fat_tree_scenario_args.scale == "fat-tree-k8"
    assert fat_tree_benchmark_args.scales == "fat-tree-k8,fat-tree-k12"
