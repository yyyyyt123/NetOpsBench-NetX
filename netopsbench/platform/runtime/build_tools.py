"""Build the pinned runtime binaries without replacing the system Containerlab."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import tarfile
import tempfile
import urllib.request
from pathlib import Path

from .toolchain import ASSETS, DEFAULT_TOOLS

TAGS = "podman,exclude_graphdriver_btrfs,btrfs_noversion,exclude_graphdriver_devicemapper,exclude_graphdriver_overlay,containers_image_openpgp"


def _main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, help="Use a previously downloaded pinned source archive")
    parser.add_argument("--go", default="go", help="Go 1.25+ executable")
    parser.add_argument("--output", type=Path, default=DEFAULT_TOOLS)
    parser.add_argument("--work-dir", type=Path, help="Retain patched source and Go test results here")
    parser.add_argument("--test", action="store_true", help="Run focused Go tests with the race detector")
    args = parser.parse_args()
    lock = json.loads((ASSETS / "containerlab.lock.json").read_text())
    patch = ASSETS / "containerlab.patch"
    if hashlib.sha256(patch.read_bytes()).hexdigest() != lock["patch_sha256"]:
        raise RuntimeError("Containerlab patch checksum mismatch")
    with tempfile.TemporaryDirectory(prefix="netopsbench-build-") as temp:
        work = args.work_dir.resolve() if args.work_dir else Path(temp) / "work"
        work.mkdir(parents=True, exist_ok=False)
        archive = args.archive or Path(temp) / "source.tar.gz"
        if args.archive is None:
            # Source is pinned and checked before extraction; no remote installer execution.
            urllib.request.urlretrieve(lock["archive_url"], archive)
        if hashlib.sha256(archive.read_bytes()).hexdigest() != lock["archive_sha256"]:
            raise RuntimeError("Pinned Containerlab source checksum mismatch")
        with tarfile.open(archive) as tar:
            tar.extractall(work, filter="data")
        source = work / ("containerlab-" + lock["commit"])
        # Isolate git apply from an enclosing checkout (including ignored build dirs).
        subprocess.run(["git", "init", "-q"], cwd=source, check=True)
        subprocess.run(["git", "apply", "--check", str(patch)], cwd=source, check=True)
        subprocess.run(["git", "apply", str(patch)], cwd=source, check=True)
        go = shutil.which(args.go)
        if go is None:
            raise RuntimeError("Go 1.25+ is required to build the runtime tools")
        env = {**os.environ, "CGO_ENABLED": "0", "GOTOOLCHAIN": "local"}
        flags = (
            f"-X github.com/srl-labs/containerlab/cmd.Version={lock['version']} "
            f"-X github.com/srl-labs/containerlab/cmd.commit={lock['commit']} "
            "-X github.com/srl-labs/containerlab/links.DirectVethDefault=both "
            "-X github.com/srl-labs/containerlab/nodes/sonic.DataEndpointDownDefault=on"
        )
        for name, package in [("containerlab", "."), ("mgmt-link", "./tools/mgmt-link")]:
            subprocess.run(
                [
                    go,
                    "build",
                    "-p",
                    "16",
                    "-trimpath",
                    "-tags",
                    TAGS,
                    "-ldflags",
                    flags,
                    "-o",
                    str(work / name),
                    package,
                ],
                cwd=source,
                env=env,
                check=True,
            )
        if args.test:
            subprocess.run(
                [
                    go,
                    "test",
                    "-p",
                    "16",
                    "-race",
                    "-tags",
                    TAGS,
                    "./core",
                    "./links",
                    "./runtime/docker",
                    "./nodes/sonic",
                    "./internal/mgmtlink",
                    "-run",
                    "Test(NodeQuer|Hosts|ProfileTransport|Direct|Deferred|Parking|Management|Parse|Validate|DefaultRoute|RejectUnowned)",
                    "-count=1",
                    "-timeout=180s",
                ],
                cwd=source,
                env={**env, "CGO_ENABLED": "1"},
                check=True,
            )
        args.output.mkdir(parents=True, exist_ok=True)
        manifest = {
            **lock,
            "direct_veth_mode": "both",
            "sonic_deferred_endpoints": True,
            "binaries": {
                name: hashlib.sha256((work / name).read_bytes()).hexdigest() for name in ("containerlab", "mgmt-link")
            },
        }
        # Publish the compatibility marker last: incomplete builds cannot be selected.
        (args.output / "manifest.json").unlink(missing_ok=True)
        for name in ("containerlab", "mgmt-link"):
            temporary = args.output / (name + ".tmp")
            shutil.copyfile(work / name, temporary)
            temporary.chmod(0o755)
            temporary.replace(args.output / name)
        (args.output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
        print(f"Built pinned runtime tools in {args.output.resolve()}")


if __name__ == "__main__":
    _main()
