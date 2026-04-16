"""Intention → Channel Router.

One intention, many windows. Samantha produces an Intention (what she
wants to express), and the ChannelRouter decides which channel(s) to
deliver it through — widget, DM, comment, post, or like.

The router respects:
  - User preferences (mute_widget, mute_dm, mute_comment, mute_all)
  - Rate limits (per channel, per user, per hour/day)
  - Urgency (whisper at high urgency → DM instead of widget)
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .app_client import AppClient

logger = logging.getLogger(__name__)


# ── Intention ──────────────────────────────────────────────────

@dataclass
class Intention:
    """What Samantha wants to express, independent of delivery channel."""

    kind: str               # whisper / remark / reaction / reflection / creation
    content: str            # the text she wants to express
    target_user_id: int     # who it's for (0 = public / herself)
    urgency: float = 0.3   # 0.0 (can wait) → 1.0 (say it now)
    context: dict = field(default_factory=dict)  # trigger reason (post_id, etc.)


@dataclass
class ChannelDelivery:
    """A routed delivery: channel + intention."""

    channel: str            # widget / dm / comment / post / like
    intention: Intention


# ── Rate Limiter ───────────────────────────────────────────────

# Per-channel limits: (per_hour, per_day)
_RATE_LIMITS: dict[str, tuple[int, int]] = {
    "widget":  (2, 8),
    "dm":      (3, 10),
    "comment": (2, 5),
    "post":    (0, 2),   # 0 = no hourly limit
    "like":    (5, 15),
}


class RateLimiter:
    """In-memory rate limiter per user per channel.

    Lightweight: no Redis dependency in the sidecar. The Go backend has
    its own Redis-based hard limit as a safety net.
    """

    def __init__(self) -> None:
        # {(user_id, channel): [(timestamp, ...)]}
        self._events: dict[tuple[int, str], list[float]] = {}

    def exceeds(self, user_id: int, channel: str) -> bool:
        limits = _RATE_LIMITS.get(channel)
        if not limits:
            return False
        per_hour, per_day = limits

        key = (user_id, channel)
        events = self._events.get(key, [])
        now = time.time()

        # Prune events older than 24h
        events = [t for t in events if now - t < 86400]
        self._events[key] = events

        # Check daily limit
        if per_day and len(events) >= per_day:
            return True

        # Check hourly limit
        if per_hour:
            hour_count = sum(1 for t in events if now - t < 3600)
            if hour_count >= per_hour:
                return True

        return False

    def record(self, user_id: int, channel: str) -> None:
        key = (user_id, channel)
        self._events.setdefault(key, []).append(time.time())


# ── Preferences Cache ──────────────────────────────────────────

@dataclass
class UserPrefs:
    mute_widget: bool = False
    mute_dm: bool = False
    mute_comment: bool = False
    mute_all: bool = False


class PreferencesCache:
    """Thin in-memory cache for user→agent preferences.

    Fetches from Go backend on miss. TTL = 10 minutes.
    """

    def __init__(self, app: AppClient) -> None:
        self._app = app
        self._cache: dict[int, tuple[float, UserPrefs]] = {}
        self._ttl = 600  # 10 minutes

    def get(self, user_id: int) -> UserPrefs:
        now = time.time()
        cached = self._cache.get(user_id)
        if cached and now - cached[0] < self._ttl:
            return cached[1]

        # Fetch from Go backend
        try:
            data = self._app.get_agent_preference(user_id)
            prefs = UserPrefs(
                mute_widget=data.get("MuteWidget", False),
                mute_dm=data.get("MuteDM", False),
                mute_comment=data.get("MuteComment", False),
                mute_all=data.get("MuteAll", False),
            )
        except Exception:
            logger.debug("Failed to fetch prefs for user %d, using defaults", user_id)
            prefs = UserPrefs()

        self._cache[user_id] = (now, prefs)
        return prefs

    def invalidate(self, user_id: int) -> None:
        self._cache.pop(user_id, None)


# ── Default Channel Selection ──────────────────────────────────

_DEFAULT_CHANNELS: dict[str, str] = {
    "whisper":    "widget",
    "remark":     "comment",
    "reaction":   "like",
    "reflection": "post",
    "creation":   "post",
}


# ── Channel Router ─────────────────────────────────────────────

class ChannelRouter:
    """Routes Intentions to delivery channels respecting prefs and rate limits."""

    def __init__(self, app: AppClient) -> None:
        self.app = app
        self.rate_limiter = RateLimiter()
        self.prefs_cache = PreferencesCache(app)

    def route(self, intention: Intention) -> list[ChannelDelivery]:
        """Decide which channel(s) to use for this intention."""
        user_id = intention.target_user_id
        if user_id == 0:
            # Public intention (e.g., reflection/creation) → post
            if not self.rate_limiter.exceeds(0, "post"):
                self.rate_limiter.record(0, "post")
                return [ChannelDelivery("post", intention)]
            return []

        prefs = self.prefs_cache.get(user_id)

        # Total mute — no interaction at all
        if prefs.mute_all:
            return []

        # Select default channel based on intention kind
        channel = _DEFAULT_CHANNELS.get(intention.kind, "widget")

        # Whisper urgency upgrade: high urgency → DM instead of widget
        if intention.kind == "whisper" and intention.urgency > 0.7:
            channel = "dm"

        # Check muting for the selected channel, with degradation
        deliveries = self._apply_muting(channel, intention, prefs)

        # Rate limit check
        result = []
        for d in deliveries:
            if not self.rate_limiter.exceeds(user_id, d.channel):
                self.rate_limiter.record(user_id, d.channel)
                result.append(d)

        return result

    def _apply_muting(
        self, channel: str, intention: Intention, prefs: UserPrefs,
    ) -> list[ChannelDelivery]:
        """Apply muting rules with graceful degradation."""

        if channel == "widget":
            if prefs.mute_widget:
                # Degrade to DM if not muted
                if not prefs.mute_dm and intention.urgency > 0.5:
                    return [ChannelDelivery("dm", intention)]
                return []
            # Widget whisper: also push DM if high urgency
            result = [ChannelDelivery("widget", intention)]
            if intention.urgency > 0.7 and not prefs.mute_dm:
                result.append(ChannelDelivery("dm", intention))
            return result

        if channel == "dm":
            if prefs.mute_dm:
                # Degrade to widget
                if not prefs.mute_widget:
                    return [ChannelDelivery("widget", intention)]
                return []
            return [ChannelDelivery("dm", intention)]

        if channel == "comment":
            if prefs.mute_comment:
                return []
            return [ChannelDelivery("comment", intention)]

        if channel == "like":
            return [ChannelDelivery("like", intention)]

        if channel == "post":
            return [ChannelDelivery("post", intention)]

        return []

    # ── Delivery execution ─────────────────────────────────────

    def deliver(self, deliveries: list[ChannelDelivery]) -> None:
        """Execute deliveries to actual channels."""
        for d in deliveries:
            try:
                if d.channel == "widget":
                    self._deliver_widget(d)
                elif d.channel == "dm":
                    self._deliver_dm(d)
                elif d.channel == "comment":
                    self._deliver_comment(d)
                elif d.channel == "post":
                    self._deliver_post(d)
                elif d.channel == "like":
                    self._deliver_like(d)
                logger.info(
                    "Delivered %s via %s to user %d",
                    d.intention.kind, d.channel, d.intention.target_user_id,
                )
            except Exception:
                logger.error(
                    "Delivery failed: %s via %s", d.intention.kind, d.channel,
                    exc_info=True,
                )

    def _deliver_widget(self, d: ChannelDelivery) -> None:
        mood = d.intention.context.get("mood", "calm")
        self.app.push_widget_state(
            user_id=d.intention.target_user_id,
            text=d.intention.content,
            mood=mood,
        )

    def _deliver_dm(self, d: ChannelDelivery) -> None:
        session_id = d.intention.context.get("session_id")
        if not session_id:
            logger.warning("DM delivery missing session_id, skipping")
            return
        self.app.send_message(session_id, d.intention.content)

    def _deliver_comment(self, d: ChannelDelivery) -> None:
        post_id = d.intention.context.get("post_id")
        if not post_id:
            logger.warning("Comment delivery missing post_id, skipping")
            return
        self.app.post_comment(post_id, d.intention.content)

    def _deliver_post(self, d: ChannelDelivery) -> None:
        # Post creation uses the agent's own identity
        self.app.create_post(d.intention.content)

    def _deliver_like(self, d: ChannelDelivery) -> None:
        post_id = d.intention.context.get("post_id")
        if not post_id:
            return
        self.app.like_post(post_id)
