"""Legacy Oasyce App adapter kept inside Samantha for compatibility.

The adapter class is intentionally kept thin; concrete App behavior
lives in `legacy_app_surface.py` and `legacy_app_tools.py` so the whole
surface can be migrated out later with minimal core churn.
"""

from __future__ import annotations

from ..app_client import AppClient
from ..channel import AppChannel
from .base import AdapterCapabilities, AdapterConfig, SurfaceAdapter
from .legacy_app_surface import (
    collect_legacy_app_stimuli,
    deliver_legacy_app_proactive,
    enrich_legacy_app_context,
    fetch_legacy_app_history,
    fetch_legacy_app_user_posts,
    format_legacy_app_stimulus,
    inject_legacy_app_tool_defaults,
    scan_legacy_app_inputs,
    start_legacy_app_runtime,
)
from .legacy_app_tools import register_legacy_app_tools


class LegacyAppAdapter(SurfaceAdapter):
    """Compatibility adapter for the existing App deployment."""

    adapter_id = "app-legacy"
    capabilities = AdapterCapabilities(
        chat=True,
        social_feed=True,
        public_posting=True,
        ephemeral_presence=True,
    )

    def __init__(self, config: AdapterConfig) -> None:
        super().__init__(config)
        self.app = AppClient(
            config.options.get("app_api_base", ""),
            config.options.get("jwt_token", ""),
        )
        self._router = None

    def make_channel(self, runtime):
        return AppChannel(self.app)

    def start(self, runtime) -> None:
        start_legacy_app_runtime(runtime)

    def contribute_tools(self, registry) -> None:
        register_legacy_app_tools(registry)

    def enrich(self, runtime, stimulus, plan, ctx) -> None:
        enrich_legacy_app_context(self.app, stimulus, ctx)

    def format_stimulus(self, stimulus) -> str | None:
        return format_legacy_app_stimulus(stimulus)

    def inject_tool_defaults(self, tool_call, stimulus: Stimulus) -> None:
        inject_legacy_app_tool_defaults(tool_call, stimulus)

    def fetch_user_posts(self, runtime, user_id: int) -> list[dict]:
        return fetch_legacy_app_user_posts(self.app, user_id)

    def fetch_history(self, runtime, stimulus):
        return fetch_legacy_app_history(self.app, runtime, stimulus)

    def scan_proactive_inputs(self, runtime, seen_posts: set[int], seen_comments: set[int]) -> None:
        scan_legacy_app_inputs(self.app, runtime, seen_posts, seen_comments)

    def collect_feed_stimuli(self, runtime, seen_posts: set[int], seen_comments: set[int]):
        return collect_legacy_app_stimuli(
            self.app, seen_posts, seen_comments, runtime.config.user_id,
        )

    def contribute_streams(self, runtime) -> list:
        from ..streams import FeedStream
        return [FeedStream(runtime, interval=runtime.config.proactive_interval)]

    def deliver_proactive(
        self,
        runtime,
        user_id: int,
        content: str,
        urgency: float = 0.3,
        context: dict | None = None,
    ) -> bool:
        if self._router is None:
            from ..intention import ChannelRouter

            self._router = ChannelRouter(self.app)
        return deliver_legacy_app_proactive(
            self._router,
            user_id,
            content,
            urgency,
            context,
        )
