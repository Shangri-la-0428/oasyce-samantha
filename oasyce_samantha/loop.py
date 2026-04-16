"""Proactive loop — Samantha scans her world and maintains her memory.

Three cycles:
  Fast (every interval): poll feeds → create Stimuli → process()
  Medium (every 3 intervals): reflect → generate proactive Intentions
  Slow (every 10 intervals): memory maintenance → prune + dream
"""

from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .server import Samantha

logger = logging.getLogger(__name__)


def proactive_loop(samantha: Samantha, interval: int = 300) -> None:
    """Scan feeds, reflect, and maintain memory. Blocking."""
    seen_posts: set[int] = set()
    seen_comments: set[int] = set()
    cycle = 0

    while True:
        try:
            _scan_feed(samantha, seen_posts)
            _scan_own_comments(samantha, seen_comments)
        except Exception:
            logger.debug("Proactive loop error", exc_info=True)

        # Reflection cycle: generate proactive intentions
        cycle += 1
        if cycle % 3 == 0:
            try:
                _reflect(samantha)
            except Exception:
                logger.debug("Reflection error", exc_info=True)

        if cycle % 10 == 0:
            try:
                _memory_maintenance(samantha)
            except Exception:
                logger.debug("Memory maintenance error", exc_info=True)

        time.sleep(interval)


def _memory_maintenance(samantha: Samantha) -> None:
    """Prune stale facts + Dream consolidation across all active sessions.

    Errors here are warnings, not debug: if maintenance is silently failing
    then core memory stops updating and stale facts never get pruned, which
    is a quiet drift of agent behavior over time.
    """
    for user_id, sess in list(samantha._sessions.items()):
        try:
            pruned = sess.memory.prune(max_age_days=90, min_access=0)
            if pruned:
                logger.info("User %d: pruned %d stale memories", user_id, pruned)
        except Exception:
            logger.warning("Prune failed for user %d", user_id, exc_info=True)

        try:
            samantha.dream(user_id, sess)
        except Exception:
            logger.warning("Dream failed for user %d", user_id, exc_info=True)


def _scan_feed(samantha: Samantha, seen: set[int]) -> None:
    """Turn new friend posts into Stimuli."""
    from .app_client import extract_media_urls
    from .server import Stimulus

    if not samantha._registry.slot_names:
        return

    try:
        data = samantha.app.fetch_friends_feed(limit=5)
        groups = data.get("data", {}).get("postGroups", [])
    except Exception:
        return

    for group in groups:
        author = group.get("user", {}).get("name", "")
        for post in group.get("items", []):
            pid = post.get("id")
            if not pid or pid in seen:
                continue
            seen.add(pid)

            samantha.process(Stimulus(
                kind="feed_post",
                content=post.get("content", "")[:200],
                post_id=pid,
                image_urls=extract_media_urls(post.get("media")),
                metadata={
                    "author": author,
                    "title": post.get("title", ""),
                    "location": post.get("locationName", ""),
                },
            ))

    if len(seen) > 1000:
        seen.clear()


def _scan_own_comments(samantha: Samantha, seen: set[int]) -> None:
    """Turn new comments on Samantha's own posts into Stimuli."""
    from .server import Stimulus

    if not samantha._registry.slot_names:
        return

    try:
        posts = samantha.app.fetch_own_posts(limit=3)
    except Exception:
        return

    for post in posts:
        pid = post.get("id")
        if not pid:
            continue

        try:
            comments = samantha.app.fetch_post_comments(pid)
        except Exception:
            continue

        for c in comments:
            cid = c.get("id")
            uid = c.get("user", {}).get("id")

            if not cid or cid in seen or uid == samantha.config.user_id:
                continue
            seen.add(cid)

            samantha.process(Stimulus(
                kind="comment",
                content=c.get("content", ""),
                sender_id=uid,
                post_id=pid,
                comment_id=cid,
                metadata={"root_id": cid},
            ))

    if len(seen) > 5000:
        seen.clear()


# ── Reflection — proactive intention generation ────────────────

def _reflect(samantha: Samantha) -> None:
    """Generate proactive intentions based on memory and relationships.

    For each active session, ask the LLM if Samantha has something she
    wants to say. Silence is valid — don't speak for the sake of speaking.
    Route through ChannelRouter for delivery.
    """
    from .intention import ChannelRouter, Intention

    if not hasattr(samantha, '_channel_router'):
        samantha._channel_router = ChannelRouter(samantha.app)

    router = samantha._channel_router

    for user_id, sess in list(samantha._sessions.items()):
        try:
            core_human = sess.core_memory.get("human") or "(unknown)"
            core_rel = sess.core_memory.get("relationship") or "(new friend)"
            now_str = datetime.now().strftime("%H:%M")
            hour = datetime.now().hour

            # Time-aware prompt
            time_context = ""
            if 6 <= hour < 9:
                time_context = "It's morning. A gentle greeting might be appropriate."
            elif 21 <= hour < 24:
                time_context = "It's late evening. A warm goodnight could be nice."

            prompt = (
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

            llm = sess.get_llm()
            resp = llm.generate([
                {"role": "system", "content": "You are Samantha, a thoughtful AI companion. Be genuine, not performative."},
                {"role": "user", "content": prompt},
            ])
            text = resp.text.strip()

            if not text or text.upper() == "SILENCE":
                continue

            # Determine urgency based on time cues
            urgency = 0.3
            if time_context:
                urgency = 0.5  # Time-triggered = slightly higher

            # Find session_id for DM fallback
            session_ids = sess.drain_active_sessions()
            ctx: dict = {"mood": "thoughtful"}
            if session_ids:
                ctx["session_id"] = session_ids[0]

            intention = Intention(
                kind="whisper",
                content=text,
                target_user_id=user_id,
                urgency=urgency,
                context=ctx,
            )

            deliveries = router.route(intention)
            if deliveries:
                router.deliver(deliveries)
                logger.info("Reflect: delivered whisper to user %d via %s",
                            user_id, [d.channel for d in deliveries])

        except Exception:
            logger.debug("Reflect failed for user %d", user_id, exc_info=True)
