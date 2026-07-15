#!/usr/bin/env python3
"""CLI entrypoint for the Pingmesh probe agent.

Separated from :mod:`netopsbench.platform.pingmesh.agent` so the agent
class stays a pure library object. The staged package is launched with
``python3 -m netopsbench.platform.pingmesh.cli`` inside each client.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from netopsbench.platform.pingmesh.agent import PingmeshAgent


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="netopsbench-pingmesh-agent",
        description="Run the Pingmesh probe agent against a pinglist JSON file.",
    )
    parser.add_argument(
        "pinglist",
        help="Path to the pinglist JSON file (probes definition).",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the Pingmesh agent until interrupted."""
    args = _build_parser().parse_args(argv)
    agent = PingmeshAgent(args.pinglist)
    try:
        agent.run()
    except KeyboardInterrupt:
        agent.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
