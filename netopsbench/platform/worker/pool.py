"""Worker pool runtime helpers."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from netopsbench.platform.worker.common import (
    _SCALE_SUBNET_BASE,
    _SCALE_SUBNET_PREFIX,
    WorkerRuntimeError,
    _safe_label,
)


@dataclass
class WorkerSpec:
    """Runtime description for one isolated worker lab."""

    index: int
    lab_name: str
    topology_dir: str
    mgmt_subnet: str
    bucket: str
    shard_dir: str
    report_path: str
    log_path: str
    deploy_log_path: str
    id: str | None = None
    name: str | None = None
    root_dir: Path | None = None
    reuse_source: str | None = None
    reused_existing: bool = False
    redeployed: bool = False
    scenarios: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.id is None:
            self.id = self.name or f"worker-{self.index}"
        if self.name is None:
            self.name = self.id
        if self.root_dir is not None and not isinstance(self.root_dir, Path):
            self.root_dir = Path(self.root_dir)


def _worker_bucket(scale: str, worker_index: int) -> str:
    return f"network_data_{_safe_label(scale)}_w{worker_index:02d}"


def _worker_mgmt_subnet_prefix(scale: str) -> int:
    return _SCALE_SUBNET_PREFIX.get(scale, 24)


def _worker_mgmt_subnet_stride(scale: str) -> int:
    prefix = _worker_mgmt_subnet_prefix(scale)
    return 1 if prefix >= 24 else 2 ** (24 - prefix)


def _worker_mgmt_subnet(scale: str, worker_index: int) -> str:
    prefix = _worker_mgmt_subnet_prefix(scale)
    stride = _worker_mgmt_subnet_stride(scale)
    if prefix == 24:
        third_octet = _SCALE_SUBNET_BASE.get(scale, 200) + worker_index
    else:
        third_octet = _SCALE_SUBNET_BASE.get(scale, 200) + ((worker_index - 1) * stride)
    if third_octet + stride - 1 > 254:
        raise WorkerRuntimeError(f"Worker index {worker_index} exceeds available management subnet range")
    return f"172.31.{third_octet}.0/{prefix}"
