"""Fault extension contracts independent from registry and builtin packs."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class ActiveFault:
    """Normalized representation of an active injected fault."""

    type: str
    device: str | None = None
    interface: str | None = None
    success: bool = True
    error: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __getitem__(self, key: str) -> Any:
        return self.to_dict()[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self.to_dict().get(key, default)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "type": self.type,
            "success": self.success,
            "error": self.error,
        }
        if self.device is not None:
            payload["device"] = self.device
        if self.interface is not None:
            payload["interface"] = self.interface
        payload.update(dict(self.metadata or {}))
        return payload

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> ActiveFault:
        metadata = dict(payload or {})
        fault_type = metadata.pop("type")
        device = metadata.pop("device", None)
        interface = metadata.pop("interface", None)
        success = metadata.pop("success", True)
        error = metadata.pop("error", None)
        return cls(
            type=fault_type,
            device=device,
            interface=interface,
            success=success,
            error=error,
            metadata=metadata,
        )


@dataclass
class FaultSpec:
    name: str
    inject_episode: Callable[[Any, Any], dict[str, Any]] | None = None
    recover_active_fault: Callable[[Any, dict[str, Any]], dict[str, Any]] | None = None
    requires_interface: bool = False
    requires_prefix: bool = False
    required_parameters: tuple[str, ...] = ()
    episode_validator: Callable[[Any], list[str]] | None = None
    aliases: list[str] = field(default_factory=list)
    scenario_supported: bool = True

    def validate_episode(self, episode: Any, episode_index: int | None = None) -> list[str]:
        errors: list[str] = []
        prefix = f"Episode {episode_index}: " if episode_index is not None else ""
        if self.requires_interface and not getattr(episode, "target_interface", None):
            errors.append(f"{prefix}{self.name} requires target_interface")
        if self.requires_prefix and not getattr(episode, "target_prefix", None):
            errors.append(f"{prefix}{self.name} requires target_prefix")
        parameters = getattr(episode, "parameters", {}) or {}
        metadata = getattr(episode, "metadata", {}) or {}
        for key in self.required_parameters:
            value = parameters.get(key, metadata.get(key))
            if value in (None, "", [], {}, ()):
                errors.append(f"{prefix}{self.name} requires parameter '{key}'")
        if self.episode_validator is not None:
            errors.extend(self.episode_validator(episode))
        return errors


class FaultExecutor(Protocol):
    def inject(self, context: Any) -> Any: ...

    def recover(self, context: Any) -> Any: ...


class FaultPack(Protocol):
    name: str
    version: str | None

    def register(self, registry: Any) -> None: ...


__all__ = ["ActiveFault", "FaultExecutor", "FaultPack", "FaultSpec"]
