"""Samantha deployment adapters."""

from .base import (
    AdapterCapabilities,
    AdapterConfig,
    AdapterLoader,
    SurfaceAdapter,
)
from .legacy_app import LegacyAppAdapter
from .local import LocalAdapter

__all__ = [
    "AdapterCapabilities",
    "AdapterConfig",
    "AdapterLoader",
    "LegacyAppAdapter",
    "LocalAdapter",
    "SurfaceAdapter",
]
