"""Canonical persisted schemas shared across NetOpsBench layers."""

from .profiles import SCALE_PROFILES, ScaleProfile, get_scale_profile, supported_scales
from .runtime import RuntimeIdentity, safe_runtime_label
from .topology import (
    SCHEMA_VERSION,
    Collector,
    Device,
    DeviceRole,
    Link,
    LinkEndpoint,
    Management,
    PingmeshPolicy,
    TopologyManifest,
)

__all__ = [
    "SCHEMA_VERSION",
    "Collector",
    "Device",
    "DeviceRole",
    "Link",
    "LinkEndpoint",
    "Management",
    "PingmeshPolicy",
    "RuntimeIdentity",
    "SCALE_PROFILES",
    "ScaleProfile",
    "TopologyManifest",
    "get_scale_profile",
    "safe_runtime_label",
    "supported_scales",
]
