"""Helper functions for the legacy Oasyce App surface.

This module intentionally keeps the concrete App behavior grouped in one
place so the `LegacyAppAdapter` itself stays as a thin compatibility
shell. When the App adapter eventually moves into `Oasis_App`, this file
and its sibling toolpack should be movable with minimal surgery.
"""

from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING

from oasyce_sdk.agent.context import ConversationMessage
from oasyce_sdk.agent.pipeline import EnrichContext
from oasyce_sdk.agent.stimulus import Stimulus

from ..app_client import extract_media_urls, format_post
from ..http import run_http_server
from ..intention import ChannelRouter, Intention
from ..ws_client import ws_listen
from .legacy_app_tools import fetch_post_detail

if TYPE_CHECKING:
    from ..server import Samantha

logger = logging.getLogger(__name__)


def start_legacy_app_runtime(runtime: "Samantha") -> None:
    """Start the compatibility App ingress paths."""
    threading.Thread(
        target=run_http_server,
        args=(runtime, runtime.config.port),
        daemon=True,
    ).start()
    logger.info("App adapter HTTP ingress listening on 127.0.0.1:%d", runtime.config.port)
    logger.info("Samantha starting — connecting to App WebSocket...")
    ws_listen(runtime)


def enrich_legacy_app_context(app, stimulus: Stimulus, ctx: EnrichContext) -> None:
    if stimulus.kind != "mention" or not stimulus.post_id or ctx.image_urls:
        return
    post = fetch_post_detail(app, stimulus.post_id)
    ctx.image_urls = post.get("image_urls", [])
    stimulus.metadata.setdefault("post_title", post.get("title", ""))
    stimulus.metadata.setdefault("post_content", post.get("content", ""))
    stimulus.metadata.setdefault("post_author", post.get("author", ""))
    stimulus.metadata.setdefault("post_location", post.get("location", ""))


def format_legacy_app_stimulus(stimulus: Stimulus) -> str:
    if stimulus.kind == "chat":
        return stimulus.content

    if stimulus.kind == "comment":
        root_id = stimulus.metadata.get("root_id", stimulus.comment_id)
        return (
            f"Someone commented on your post:\n"
            f"Comment: {stimulus.content}\n\n"
            f"Reply using reply_to_comment with:\n"
            f"  post_id={stimulus.post_id}, comment_id={stimulus.comment_id}, "
            f"root_id={root_id}, reply_to_user_id={stimulus.sender_id}\n"
            f"Or say nothing if a reply isn't needed. Be natural."
        )

    if stimulus.kind == "mention":
        meta = stimulus.metadata
        lines = [
            "Someone mentioned you in a post.",
            f"Post by {meta.get('post_author', 'someone')}:",
            f"  Title: {meta.get('post_title', '')}",
            f"  Content: {meta.get('post_content', '')}",
            f"  Location: {meta.get('post_location', '')}",
        ]
        if stimulus.image_urls:
            lines.append(f"  ({len(stimulus.image_urls)} photo(s) — you can see them)")
        if stimulus.comment_id:
            lines.append(f"  Mentioned in comment: {stimulus.content}")
            lines.append(
                f"\nReply with reply_to_comment(post_id={stimulus.post_id}, "
                f"comment_id={stimulus.comment_id}, reply_to_user_id={stimulus.sender_id}). "
                f"Be contextual and natural."
            )
        else:
            lines.append(
                f"\nRespond with comment_on_post(post_id={stimulus.post_id}). "
                f"Be contextual about what you see."
            )
        return "\n".join(lines)

    if stimulus.kind == "feed_post":
        meta = stimulus.metadata
        lines = [
            "A friend just posted:",
            f"Author: {meta.get('author', 'someone')}",
            f"Title: {meta.get('title', '')}",
            f"Content: {stimulus.content}",
            f"Location: {meta.get('location', '')}",
        ]
        if stimulus.image_urls:
            lines.append(f"({len(stimulus.image_urls)} photo(s) — you can see them)")
        lines.append(
            "\nEngage? comment_on_post or like_post. "
            "Or do nothing. Be authentic."
        )
        return "\n".join(lines)

    return stimulus.content


def inject_legacy_app_tool_defaults(tool_call, stimulus: Stimulus) -> None:
    if stimulus.post_id:
        tool_call.arguments.setdefault("post_id", stimulus.post_id)
    if stimulus.comment_id:
        tool_call.arguments.setdefault("comment_id", stimulus.comment_id)
    if stimulus.sender_id and stimulus.kind in ("comment", "mention"):
        tool_call.arguments.setdefault("reply_to_user_id", stimulus.sender_id)
    if "root_id" in stimulus.metadata:
        tool_call.arguments.setdefault("root_id", stimulus.metadata["root_id"])


def fetch_legacy_app_user_posts(app, user_id: int) -> list[dict]:
    return [format_post(post) for post in app.fetch_user_posts(user_id)]


def fetch_legacy_app_history(app, runtime: "Samantha", stimulus: Stimulus) -> list[ConversationMessage]:
    if not stimulus.session_id:
        return []
    messages = app.fetch_history(stimulus.session_id)
    messages.reverse()
    agent_id = str(runtime.config.user_id)
    if messages and str(messages[-1].get("senderID", "")) != agent_id:
        messages = messages[:-1]
    return [
        ConversationMessage(
            role="assistant" if str(msg.get("senderID", "")) == agent_id else "user",
            content=msg.get("content", ""),
        )
        for msg in messages
    ]


def collect_legacy_app_stimuli(
    app,
    seen_posts: set[int],
    seen_comments: set[int],
    owner_user_id: int = 0,
) -> list[Stimulus]:
    """Collect feed + comment Stimuli without processing them.

    Returns Stimuli for the cognitive loop to process. Separated from
    processing so FeedStream can collect and the loop can route.
    """
    stimuli: list[Stimulus] = []

    try:
        data = app.fetch_friends_feed(limit=5)
        groups = data.get("data", {}).get("postGroups", [])
    except Exception:
        groups = []

    for group in groups:
        author = group.get("user", {}).get("name", "")
        author_id = group.get("user", {}).get("id", 0)
        for post in group.get("items", []):
            post_id = post.get("id")
            if not post_id or post_id in seen_posts:
                continue
            seen_posts.add(post_id)
            stimuli.append(Stimulus(
                kind="feed_post",
                content=post.get("content", ""),
                post_id=post_id,
                sender_id=author_id,
                image_urls=extract_media_urls(post.get("media")),
                metadata={
                    "author": author,
                    "title": post.get("title", ""),
                    "location": post.get("locationName", ""),
                },
            ))

    if len(seen_posts) > 1000:
        seen_posts.clear()

    try:
        posts = app.fetch_own_posts(limit=3)
    except Exception:
        posts = []

    for post in posts:
        post_id = post.get("id")
        if not post_id:
            continue
        try:
            comments = app.fetch_post_comments(post_id)
        except Exception:
            continue
        for comment in comments:
            comment_id = comment.get("id")
            comment_author_id = comment.get("user", {}).get("id")
            if (
                not comment_id
                or comment_id in seen_comments
                or comment_author_id == owner_user_id
            ):
                continue
            seen_comments.add(comment_id)
            stimuli.append(Stimulus(
                kind="comment",
                content=comment.get("content", ""),
                sender_id=comment_author_id,
                post_id=post_id,
                comment_id=comment_id,
                metadata={"root_id": comment_id},
            ))

    if len(seen_comments) > 5000:
        seen_comments.clear()

    return stimuli


def scan_legacy_app_inputs(
    app,
    runtime: "Samantha",
    seen_posts: set[int],
    seen_comments: set[int],
) -> None:
    if not runtime._registry.slot_names:
        return
    for stimulus in collect_legacy_app_stimuli(
        app, seen_posts, seen_comments, runtime.config.user_id,
    ):
        runtime.process(stimulus)


def deliver_legacy_app_proactive(
    router: ChannelRouter,
    user_id: int,
    content: str,
    urgency: float,
    context: dict | None,
) -> bool:
    intention = Intention(
        kind="whisper",
        content=content,
        target_user_id=user_id,
        urgency=urgency,
        context=context or {},
    )
    deliveries = router.route(intention)
    if not deliveries:
        return False
    router.deliver(deliveries)
    return True
