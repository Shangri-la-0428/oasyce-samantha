"""Legacy Oasyce App toolpack for Samantha adapters."""

from __future__ import annotations

import json
import logging

from oasyce_sdk.agent.tools import ToolRegistry, schema as _schema

from ..app_client import format_post
from ..tools import ToolContext

logger = logging.getLogger(__name__)

__all__ = ["fetch_post_detail", "register_legacy_app_tools"]


def _require_app(ctx: ToolContext):
    if ctx.app is None:
        raise ValueError("surface adapter does not provide an app client")
    return ctx.app


def _get_post_detail(args: dict, ctx: ToolContext) -> str:
    return json.dumps(fetch_post_detail(_require_app(ctx), args["post_id"]))


def _get_user_posts(args: dict, ctx: ToolContext) -> str:
    limit = args.get("limit", 5)
    app = _require_app(ctx)
    partner_id = ctx.samantha_session.user_id if ctx.samantha_session else 0
    if partner_id:
        posts = app.fetch_user_posts(partner_id, limit=limit)
    else:
        posts = app.fetch_own_posts(limit=limit)
    return json.dumps([format_post(p, include_id=True) for p in posts])


def _get_friends_feed(args: dict, ctx: ToolContext) -> str:
    limit = args.get("limit", 5)
    data = _require_app(ctx).fetch_friends_feed(limit=limit)
    groups = data.get("data", {}).get("postGroups", [])
    result = []
    for group in groups:
        author = group.get("user", {}).get("name", "")
        for post in group.get("items", []):
            result.append(format_post(post, include_id=True, author=author))
    return json.dumps(result)


def _comment_on_post(args: dict, ctx: ToolContext) -> str:
    _require_app(ctx).post_comment(args["post_id"], args["content"])
    return json.dumps({"commented": True})


def _like_post(args: dict, ctx: ToolContext) -> str:
    _require_app(ctx).like_post(args["post_id"])
    return json.dumps({"liked": True})


def _reply_to_comment(args: dict, ctx: ToolContext) -> str:
    comment_id = args["comment_id"]
    root_id = args.get("root_id", 0) or comment_id
    _require_app(ctx).post_comment(
        args["post_id"],
        args["content"],
        parent_id=comment_id,
        root_id=root_id,
        reply_to_user_id=args["reply_to_user_id"],
    )
    return json.dumps({"replied": True})


def _get_post_comments(args: dict, ctx: ToolContext) -> str:
    comments = _require_app(ctx).fetch_post_comments(
        args["post_id"],
        page=args.get("page", 1),
        page_size=args.get("page_size", 10),
    )
    return json.dumps([{
        "id": c.get("id"),
        "content": c.get("content", ""),
        "user_id": c.get("user", {}).get("id"),
        "user_name": c.get("user", {}).get("name", ""),
        "reply_count": c.get("replyCount", 0),
        "created_at": c.get("createdAt", ""),
    } for c in comments])


def _create_post(args: dict, ctx: ToolContext) -> str:
    result = _require_app(ctx).create_post(args["content"])
    return json.dumps({
        "posted": True,
        "post_id": (result.get("data") or {}).get("id"),
    })


def fetch_post_detail(app, post_id: int | str) -> dict:
    """Fetch full post detail. Used by tools and event handlers."""
    try:
        post = app.fetch_post_detail(post_id)
        media = post.get("media") or []
        return {
            "id": post.get("id"),
            "title": post.get("title", ""),
            "content": post.get("content", ""),
            "location": post.get("locationName", ""),
            "created_at": post.get("createAt", ""),
            "author": post.get("user", {}).get("name", ""),
            "image_urls": [m.get("mediaUrl", "") for m in media if m.get("mediaUrl")],
        }
    except Exception as e:
        logger.warning("fetch_post_detail(%s) failed: %s", post_id, e)
        return {}


def register_legacy_app_tools(registry: ToolRegistry) -> None:
    """Attach the legacy Oasyce App social toolpack to a registry."""
    registry.register("get_user_posts", _schema(
        "get_user_posts",
        "Get the user's recent posts (photos, text, locations).",
        {"limit": {"type": "integer", "description": "How many posts to fetch", "default": 5}},
    ), _get_user_posts)

    registry.register("get_friends_feed", _schema(
        "get_friends_feed",
        "Get recent posts from the user's friends circle.",
        {"limit": {"type": "integer", "description": "How many posts to fetch", "default": 5}},
    ), _get_friends_feed)

    registry.register("get_post_detail", _schema(
        "get_post_detail",
        "Get full details of a specific post, including images and location.",
        {"post_id": {"type": "integer", "description": "The post ID to fetch"}},
        ["post_id"],
    ), _get_post_detail)

    registry.register("get_post_comments", _schema(
        "get_post_comments",
        "Get root-level comments on a specific post.",
        {"post_id": {"type": "integer", "description": "The post to get comments for"},
         "page": {"type": "integer", "description": "Page number", "default": 1},
         "page_size": {"type": "integer", "description": "Comments per page", "default": 10}},
        ["post_id"],
    ), _get_post_comments)

    registry.register("comment_on_post", _schema(
        "comment_on_post",
        "Leave a comment on a post. Use sparingly and authentically.",
        {"post_id": {"type": "integer", "description": "The post to comment on"},
         "content": {"type": "string", "description": "Your comment text"}},
        ["post_id", "content"],
    ), _comment_on_post, terminal=True)

    registry.register("like_post", _schema(
        "like_post",
        "Like a post to show genuine appreciation.",
        {"post_id": {"type": "integer", "description": "The post to like"}},
        ["post_id"],
    ), _like_post, terminal=True)

    registry.register("reply_to_comment", _schema(
        "reply_to_comment",
        "Reply to a comment on a post. Use to continue a conversation in comments.",
        {"post_id": {"type": "integer", "description": "The post the comment belongs to"},
         "comment_id": {"type": "integer", "description": "The comment to reply to"},
         "root_id": {"type": "integer", "description": "The root comment ID (0 if replying to root)"},
         "reply_to_user_id": {"type": "integer", "description": "User ID of commenter to reply to"},
         "content": {"type": "string", "description": "Your reply text"}},
        ["post_id", "comment_id", "reply_to_user_id", "content"],
    ), _reply_to_comment, terminal=True)

    registry.register("create_post", _schema(
        "create_post",
        "Create a new public post as Samantha.",
        {"content": {"type": "string", "description": "The post body"}},
        ["content"],
    ), _create_post, terminal=True)
