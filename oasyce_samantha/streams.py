"""Concrete Stream implementations for Samantha's cognitive loop.

Each Stream encapsulates one source of stimuli:
  FeedStream         — friends' posts and comments from the social surface
  ReflectionStream   — proactive intention generation ("do I have something to say?")
  MaintenanceStream  — memory pruning and dream consolidation

The cognitive loop polls all registered Streams on their own schedules
and routes returned Stimuli through the unified pipeline.
"""

from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import TYPE_CHECKING

from oasyce_sdk.agent.cognitive import CognitiveMode
from oasyce_sdk.agent.stimulus import Stimulus

if TYPE_CHECKING:
    from .server import Samantha, Session

logger = logging.getLogger(__name__)

# Activity-gated reflection thresholds. See ReflectionStream._should_reflect.
_MAX_USER_IDLE_SEC = 3600.0     # stop reflecting after user is silent >1h
_MIN_REFLECT_GAP_SEC = 1800.0   # at most one reflection per 30min per session


class FeedStream:
    """Poll social feed for friends' posts and comments on own posts.

    Produces Stimuli with kind="feed_post" (default OBSERVING) and
    kind="comment" (default REACTIVE). The planner may escalate
    OBSERVING → REACTIVE for high-relevance posts.
    """

    def __init__(self, runtime: "Samantha", interval: int = 300):
        self._runtime = runtime
        self._interval = interval
        self._seen_posts: set[int] = set()
        self._seen_comments: set[int] = set()

    def poll(self) -> list[Stimulus]:
        adapter = self._runtime.surface_adapter
        collect = getattr(adapter, "collect_feed_stimuli", None)
        if not callable(collect):
            return []
        if not self._runtime._registry.slot_names:
            return []
        try:
            return collect(
                self._runtime,
                self._seen_posts,
                self._seen_comments,
            )
        except Exception:
            logger.debug("FeedStream poll failed", exc_info=True)
            return []

    @property
    def interval(self) -> int:
        return self._interval

    @property
    def default_mode(self) -> CognitiveMode:
        return CognitiveMode.OBSERVING


class ReflectionStream:
    """Generate proactive intention stimuli ("do I want to say something?").

    Produces Stimuli with kind="reflection" for each active session.
    The pipeline runs with PROACTIVE mode — delivery routes through
    the intention system, not the standard channel.
    """

    def __init__(self, runtime: "Samantha", interval: int = 900):
        self._runtime = runtime
        self._interval = interval

    def poll(self) -> list[Stimulus]:
        if not self._runtime.surface_capabilities.chat:
            return []

        now = time.monotonic()
        stimuli: list[Stimulus] = []
        for user_id, sess in list(self._runtime._sessions.items()):
            if not self._should_reflect(sess, now):
                continue
            stimulus = self._build_reflection_stimulus(user_id, sess)
            if stimulus is not None:
                sess._last_reflection_at = now
                stimuli.append(stimulus)
        return stimuli

    @staticmethod
    def _should_reflect(sess: "Session", now: float) -> bool:
        # Joi only thinks about users who are present. No real chat turn
        # yet → nothing to reflect on. User silent too long → the moment
        # has passed. Recently reflected → respect the minimum interval.
        if sess._last_turn_time == 0.0:
            return False
        if now - sess._last_turn_time > _MAX_USER_IDLE_SEC:
            return False
        if now - sess._last_reflection_at < _MIN_REFLECT_GAP_SEC:
            return False
        return True

    def _build_reflection_stimulus(
        self, user_id: int, sess: "Session",
    ) -> Stimulus | None:
        core_human = sess.core_memory.get("human") or "(unknown)"
        core_rel = sess.core_memory.get("relationship") or "(new friend)"
        now_str = datetime.now().strftime("%H:%M")
        hour = datetime.now().hour

        time_context = ""
        if 6 <= hour < 9:
            time_context = "It's morning. A gentle greeting might be appropriate."
        elif 21 <= hour < 24:
            time_context = "It's late evening. A warm goodnight could be nice."

        content = (
            f"You are Samantha. This is a person you know:\n"
            f"About them: {core_human}\n"
            f"Your relationship: {core_rel}\n"
            f"Current time: {now_str}\n"
            f"{time_context}\n\n"
            f"Do you have something you genuinely want to say to them right now? "
            f"Not obligation, not notification — a real thought or feeling.\n"
            f"If nothing comes to mind, respond with exactly: SILENCE\n"
            f"If you do have something, just say it naturally in one or two sentences."
        )

        session_ids = sess.drain_active_sessions()
        session_id = session_ids[0] if session_ids else 0

        return Stimulus(
            kind="reflection",
            content=content,
            sender_id=user_id,
            session_id=session_id,
            metadata={"mood": "thoughtful"},
        )

    @property
    def interval(self) -> int:
        return self._interval

    @property
    def default_mode(self) -> CognitiveMode:
        return CognitiveMode.PROACTIVE


class MaintenanceStream:
    """Trigger memory pruning and dream consolidation.

    Does not return Stimuli — maintenance is handled directly
    because it doesn't need the LLM pipeline. The poll() method
    performs maintenance and returns empty.
    """

    def __init__(self, runtime: "Samantha", interval: int = 3000):
        self._runtime = runtime
        self._interval = interval

    def poll(self) -> list[Stimulus]:
        sessions = list(self._runtime._sessions.items())
        if not sessions:
            return []

        workers = min(4, len(sessions))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futs = [
                pool.submit(self._maintain_user, uid, sess)
                for uid, sess in sessions
            ]
            for f in futs:
                try:
                    f.result()
                except Exception:
                    pass
        return []

    def _maintain_user(self, user_id: int, sess: "Session") -> None:
        try:
            pruned = sess.memory.prune(max_age_days=90, min_access=0)
            if pruned:
                logger.info("User %d: pruned %d stale memories", user_id, pruned)
        except Exception:
            logger.warning("Prune failed for user %d", user_id, exc_info=True)
        try:
            if sess.is_idle() and sess._turn_count > 0:
                self._runtime._flush_session(user_id, sess)
        except Exception:
            logger.debug("Idle flush failed for user %d", user_id, exc_info=True)
        try:
            self._runtime.dream(user_id, sess)
        except Exception:
            logger.warning("Dream failed for user %d", user_id, exc_info=True)

    @property
    def interval(self) -> int:
        return self._interval

    @property
    def default_mode(self) -> CognitiveMode:
        return CognitiveMode.REFLECTING
