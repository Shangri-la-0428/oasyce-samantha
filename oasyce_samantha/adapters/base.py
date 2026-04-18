"""Surface adapters for Samantha's deployment-level world integration.

`oasyce-sdk` defines the generic Agent seams. This module defines the
next layer up for Samantha specifically: how this companion runtime
connects to a concrete surface such as a local terminal or the legacy
Oasyce App sidecar transport.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass, field
from typing import Any, TYPE_CHECKING

from oasyce_sdk.agent.llm import ToolCall
from oasyce_sdk.agent.pipeline import EnrichContext
from oasyce_sdk.agent.planner import Plan
from oasyce_sdk.agent.stimulus import Stimulus
from oasyce_sdk.agent.tools import ToolRegistry

if TYPE_CHECKING:
    from ..server import Samantha, SamanthaConfig


@dataclass(frozen=True)
class AdapterCapabilities:
    """What a surface can do for Samantha."""

    chat: bool = False
    social_feed: bool = False
    public_posting: bool = False
    ephemeral_presence: bool = False


@dataclass(frozen=True)
class AdapterConfig:
    """Minimal adapter selection payload derived from SamanthaConfig."""

    name: str = "local"
    import_path: str = ""
    options: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_runtime_config(cls, config: "SamanthaConfig") -> "AdapterConfig":
        name = (config.adapter or "").strip()
        if not name:
            # Legacy configs were App-first and had no adapter field.
            if config.jwt_token or config.user_id or config.app_api_base:
                name = "app-legacy"
            else:
                name = "local"
        options = dict(config.adapter_options or {})
        if config.app_api_base:
            options.setdefault("app_api_base", config.app_api_base)
        if config.jwt_token:
            options.setdefault("jwt_token", config.jwt_token)
        if config.user_id:
            options.setdefault("user_id", config.user_id)
        return cls(
            name=name,
            import_path=(config.adapter_import or "").strip(),
            options=options,
        )


class SurfaceAdapter:
    """Deployment-level world adapter for Samantha."""

    adapter_id = "surface"
    capabilities = AdapterCapabilities()

    def __init__(self, config: AdapterConfig) -> None:
        self.config = config

    def make_channel(self, runtime: "Samantha"):
        raise NotImplementedError

    def start(self, runtime: "Samantha") -> None:
        raise NotImplementedError

    def stop(self) -> None:
        return

    def contribute_tools(self, registry: ToolRegistry) -> None:
        return

    def enrich(
        self,
        runtime: "Samantha",
        stimulus: Stimulus,
        plan: Plan,
        ctx: EnrichContext,
    ) -> None:
        return

    def format_stimulus(self, stimulus: Stimulus) -> str | None:
        return None

    def inject_tool_defaults(self, tool_call: ToolCall, stimulus: Stimulus) -> None:
        return

    def contribute_streams(self, runtime: "Samantha") -> list:
        return []


class AdapterLoader:
    """Instantiate builtin or externally provided Samantha adapters."""

    @staticmethod
    def load(config: AdapterConfig) -> SurfaceAdapter:
        if config.import_path:
            adapter = AdapterLoader._load_external(config.import_path, config)
            if not isinstance(adapter, SurfaceAdapter):
                raise TypeError(
                    f"Adapter import {config.import_path!r} did not return a SurfaceAdapter",
                )
            return adapter

        if config.name == "local":
            from .local import LocalAdapter

            return LocalAdapter(config)

        if config.name in {"app", "app-legacy"}:
            from .legacy_app import LegacyAppAdapter

            return LegacyAppAdapter(config)

        raise ValueError(f"Unknown Samantha adapter: {config.name}")

    @staticmethod
    def _load_external(import_path: str, config: AdapterConfig) -> SurfaceAdapter:
        module_name, attr_name = AdapterLoader._split_import_path(import_path)
        module = importlib.import_module(module_name)
        obj = getattr(module, attr_name)

        if isinstance(obj, type) and issubclass(obj, SurfaceAdapter):
            return obj(config)
        if callable(obj):
            adapter = obj(config)
            if isinstance(adapter, SurfaceAdapter):
                return adapter
        if isinstance(obj, SurfaceAdapter):
            return obj
        raise TypeError(
            f"Imported object {import_path!r} is not a SurfaceAdapter, factory, or subclass",
        )

    @staticmethod
    def _split_import_path(import_path: str) -> tuple[str, str]:
        if ":" in import_path:
            module_name, attr_name = import_path.split(":", 1)
            return module_name, attr_name
        module_name, _, attr_name = import_path.rpartition(".")
        if not module_name or not attr_name:
            raise ValueError(
                "adapter_import must look like 'package.module:factory' or 'package.module.Class'",
            )
        return module_name, attr_name
