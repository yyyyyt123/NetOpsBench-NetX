"""Resolve the repository-owned Containerlab toolchain, never a silent stock fallback."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ASSETS = Path(__file__).with_name("assets")
DEFAULT_TOOLS = Path(__file__).with_name("_bin")


def runtime_tools() -> Path:
    root = Path(os.environ.get("NETOPSBENCH_RUNTIME_TOOLS", str(DEFAULT_TOOLS))).expanduser().resolve()
    hint = "Build the pinned runtime tools: python -m netopsbench.platform.runtime.build_tools"
    try:
        manifest = json.loads((root / "manifest.json").read_text())
        locked = json.loads((ASSETS / "containerlab.lock.json").read_text())
        if any(manifest.get(key) != locked[key] for key in ("commit", "patch_sha256")):
            raise ValueError("Runtime toolchain does not match the packaged patch")
        if manifest.get("direct_veth_mode") != "both":
            raise ValueError("Runtime toolchain must enable both-endpoint direct creation")
        if manifest.get("sonic_deferred_endpoints") is not True:
            raise ValueError("Runtime toolchain must defer SONiC data endpoint activation")
        for name in ("containerlab", "mgmt-link"):
            if not (root / name).is_file() or not os.access(root / name, os.X_OK):
                raise ValueError(f"Missing executable: {name}")
    except (OSError, ValueError, KeyError) as exc:
        raise RuntimeError(f"{hint}. {exc}") from exc
    return root


def managed_command() -> list[str]:
    runtime_tools()
    return [sys.executable, "-m", "netopsbench.platform.runtime.management"]
