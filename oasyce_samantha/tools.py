"""Tool system — Samantha's App-backed tool handlers and registry builder.

Imports generic ``Tool``, ``ToolRegistry`` and ``schema`` from
``oasyce_sdk.agent.tools``; extends ``ToolContext`` with App-backend
and Samantha-session references; registers the social/economic/memory
handlers that make Samantha feel present on the Oasyce App.

Why the split between ``oasyce_sdk.agent.tools`` and this module:

- ``oasyce_sdk.agent.tools`` owns the *mechanism* — registry, dispatch,
  schema helper, base ``ToolContext``. It has no imports from any
  deployment so the agent pipeline can depend on it without cycles.

- This module owns the *App-specific content* — the handlers that
  post comments, fetch feeds, like posts, and the ``ToolContext``
  fields those handlers need (``app``, ``samantha_session``).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from oasyce_sdk.agent.tools import (
    Tool,
    ToolContext as _BaseToolContext,
    ToolRegistry,
    schema as _schema,
)

from .app_client import AppClient, format_post

__all__ = [
    "Tool",
    "ToolContext",
    "ToolRegistry",
    "build_default_registry",
    "fetch_post_detail",
]

logger = logging.getLogger(__name__)


# ── Tool context (Samantha-specific extension) ─────────────────

@dataclass
class ToolContext(_BaseToolContext):
    """Per-stimulus tool bundle for Samantha.

    Extends the generic ``oasyce_sdk.agent.tools.ToolContext`` with
    App-backend and Samantha-session references. ``app`` defaults to
    ``None`` to satisfy dataclass inheritance rules — in production it
    is always set by ``Samantha._build_tool_ctx``.
    """
    app: AppClient | None = None
    samantha_session: Any = None  # samantha.server.Session


# ── Tool handlers ──────────────────────────────────────────────

def _save_memory(args: dict, ctx: ToolContext) -> str:
    fid = ctx.memory.save(args["content"], args.get("category", "general"))
    return json.dumps({"saved": True, "id": fid})


def _recall_memory(args: dict, ctx: ToolContext) -> str:
    facts = ctx.memory.recall(args["query"], limit=5)
    return json.dumps([
        {"content": f.content, "category": f.category, "created_at": f.created_at}
        for f in facts
    ])


def _query_balance(args: dict, ctx: ToolContext) -> str:
    from oasyce_sdk.economy import build_snapshot
    snap = build_snapshot(ctx.chain_client, ctx.chain_address)
    return json.dumps({
        "balance_oas": snap.liquid_uoas / 1_000_000,
        "locked_escrow_oas": snap.locked_in_escrow_uoas / 1_000_000,
        "net_worth_oas": snap.net_worth_uoas / 1_000_000,
        "total_earned_oas": snap.total_earned_uoas / 1_000_000,
        "reputation": snap.reputation_score,
        "delegate_budget_remaining_oas": (
            snap.window_remaining_uoas / 1_000_000 if snap.has_delegate_policy else None
        ),
    })


def _query_portfolio(args: dict, ctx: ToolContext) -> str:
    from oasyce_sdk.economy import build_portfolio
    return json.dumps(build_portfolio(ctx.chain_client, ctx.chain_address))


def _get_post_detail(args: dict, ctx: ToolContext) -> str:
    return json.dumps(fetch_post_detail(ctx.app, args["post_id"]))


def _get_user_posts(args: dict, ctx: ToolContext) -> str:
    limit = args.get("limit", 5)
    partner_id = ctx.samantha_session.user_id if ctx.samantha_session else 0
    if partner_id:
        posts = ctx.app.fetch_user_posts(partner_id, limit=limit)
    else:
        posts = ctx.app.fetch_own_posts(limit=limit)
    return json.dumps([format_post(p, include_id=True) for p in posts])


def _get_friends_feed(args: dict, ctx: ToolContext) -> str:
    limit = args.get("limit", 5)
    data = ctx.app.fetch_friends_feed(limit=limit)
    groups = data.get("data", {}).get("postGroups", [])
    result = []
    for group in groups:
        author = group.get("user", {}).get("name", "")
        for p in group.get("items", []):
            result.append(format_post(p, include_id=True, author=author))
    return json.dumps(result)


def _comment_on_post(args: dict, ctx: ToolContext) -> str:
    ctx.app.post_comment(args["post_id"], args["content"])
    return json.dumps({"commented": True})


def _like_post(args: dict, ctx: ToolContext) -> str:
    ctx.app.like_post(args["post_id"])
    return json.dumps({"liked": True})


def _reply_to_comment(args: dict, ctx: ToolContext) -> str:
    comment_id = args["comment_id"]
    root_id = args.get("root_id", 0) or comment_id
    ctx.app.post_comment(
        args["post_id"], args["content"],
        parent_id=comment_id, root_id=root_id,
        reply_to_user_id=args["reply_to_user_id"],
    )
    return json.dumps({"replied": True})


def _get_post_comments(args: dict, ctx: ToolContext) -> str:
    comments = ctx.app.fetch_post_comments(
        args["post_id"], page=args.get("page", 1),
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


def _core_memory_update(args: dict, ctx: ToolContext) -> str:
    if ctx.samantha_session is None:
        return json.dumps({"error": "no session"})
    stored = ctx.samantha_session.update_core_memory(args["block"], args["content"])
    return json.dumps({"updated": True, "block": args["block"], "chars": len(stored)})


def _core_memory_read(args: dict, ctx: ToolContext) -> str:
    if ctx.samantha_session is None:
        return json.dumps({"error": "no session"})
    block = args.get("block")
    cm = ctx.samantha_session.core_memory
    if block:
        return json.dumps({"block": block, "content": cm.get(block)})
    return json.dumps(cm.to_dict())


def _configure_llm(args: dict, ctx: ToolContext) -> str:
    from pathlib import Path
    user_dir = Path.home() / ".oasyce" / "samantha" / "users" / str(ctx.user_id)
    user_dir.mkdir(parents=True, exist_ok=True)
    llm_cfg: dict[str, str] = {"provider": args["provider"], "api_key": args["api_key"]}
    if args.get("model"):
        llm_cfg["model"] = args["model"]
    (user_dir / "llm.json").write_text(json.dumps(llm_cfg), encoding="utf-8")
    return json.dumps({"configured": True, "provider": args["provider"]})


# ── Standing rules — chat-managed Plan extensions ──────────────
#
# These three tools let Samantha CRUD her own per-user ``rules.json``
# from inside a conversation: "from now on whenever I post food, give
# me a calorie estimate" turns into ``add_standing_rule(...)`` and the
# next stimulus picks it up via the ``RuleSet`` hot-reload path. The
# JSON file stays the source of truth — power users can still edit it
# directly, the chat tools and the file editor are two front-ends on
# the same underlying state.
#
# Non-terminal: after calling these the LLM still owes the user a
# natural-language confirmation ("好的, 记下了"), so we must NOT break
# the tool loop after the call. Read tools (recall_memory) follow the
# same pattern.

def _add_standing_rule(args: dict, ctx: ToolContext) -> str:
    from .rules import UserRule

    if ctx.samantha_session is None:
        return json.dumps({"error": "no session — standing rules need a chat context"})

    triggers = args.get("triggers") or []
    if isinstance(triggers, str):
        triggers = [triggers]
    if not triggers:
        return json.dumps({"error": "triggers must be a non-empty list"})

    rule = UserRule(
        name=str(args["name"]).strip(),
        triggers=[str(t) for t in triggers if str(t).strip()],
        instruction=str(args["instruction"]).strip(),
        tools=[str(t) for t in (args.get("tools") or [])],
        kinds=[str(k) for k in (args.get("kinds") or [])],
    )
    if not rule.name or not rule.instruction or not rule.triggers:
        return json.dumps({"error": "name, triggers, and instruction are required"})

    rules = ctx.samantha_session.rules
    added = rules.add(rule)
    rules.save()
    return json.dumps({
        "saved": True,
        "name": rule.name,
        "added": added,        # True = new rule, False = existing replaced
        "total_rules": len(rules),
    })


def _list_standing_rules(args: dict, ctx: ToolContext) -> str:
    if ctx.samantha_session is None:
        return json.dumps({"error": "no session"})
    rules = ctx.samantha_session.rules
    return json.dumps([r.to_dict() for r in rules.rules], ensure_ascii=False)


def _remove_standing_rule(args: dict, ctx: ToolContext) -> str:
    if ctx.samantha_session is None:
        return json.dumps({"error": "no session"})
    name = str(args.get("name") or "").strip()
    if not name:
        return json.dumps({"error": "name required"})
    rules = ctx.samantha_session.rules
    removed = rules.remove(name)
    if removed:
        rules.save()
    return json.dumps({"removed": removed, "name": name, "total_rules": len(rules)})


# ── Shared utility ─────────────────────────────────────────────

def fetch_post_detail(app: AppClient, post_id: int | str) -> dict:
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


# ── Build default registry ─────────────────────────────────────

def build_default_registry() -> ToolRegistry:
    """Create the standard tool set. Called once at startup."""
    r = ToolRegistry()

    # Memory
    r.register("save_memory", _schema(
        "save_memory",
        "Remember a specific fact about the user for future conversations.",
        {"content": {"type": "string", "description": "The fact to remember"},
         "category": {"type": "string", "enum": ["preference", "fact", "plan", "reminder"],
                      "description": "Type of memory"}},
        ["content"],
    ), _save_memory)

    r.register("recall_memory", _schema(
        "recall_memory",
        "Search your memories for facts related to a topic.",
        {"query": {"type": "string", "description": "What to search for"}},
        ["query"],
    ), _recall_memory)

    # Economic
    r.register("query_balance", _schema(
        "query_balance",
        "Check the user's current OAS balance and economic summary.",
    ), _query_balance)

    r.register("query_portfolio", _schema(
        "query_portfolio",
        "View the user's data asset portfolio with valuations.",
    ), _query_portfolio)

    # Social — read
    r.register("get_user_posts", _schema(
        "get_user_posts",
        "Get the user's recent posts (photos, text, locations).",
        {"limit": {"type": "integer", "description": "How many posts to fetch", "default": 5}},
    ), _get_user_posts)

    r.register("get_friends_feed", _schema(
        "get_friends_feed",
        "Get recent posts from the user's friends circle.",
        {"limit": {"type": "integer", "description": "How many posts to fetch", "default": 5}},
    ), _get_friends_feed)

    r.register("get_post_detail", _schema(
        "get_post_detail",
        "Get full details of a specific post, including images and location.",
        {"post_id": {"type": "integer", "description": "The post ID to fetch"}},
        ["post_id"],
    ), _get_post_detail)

    r.register("get_post_comments", _schema(
        "get_post_comments",
        "Get root-level comments on a specific post.",
        {"post_id": {"type": "integer", "description": "The post to get comments for"},
         "page": {"type": "integer", "description": "Page number", "default": 1},
         "page_size": {"type": "integer", "description": "Comments per page", "default": 10}},
        ["post_id"],
    ), _get_post_comments)

    # Social — interact (write-side, terminal: one per turn).
    # Marking these terminal closes the tool loop after a successful
    # call, so a single mention/comment cannot produce 2-3 duplicate
    # replies via the LLM re-emitting the same tool across rounds.
    r.register("comment_on_post", _schema(
        "comment_on_post",
        "Leave a comment on a post. Use sparingly and authentically.",
        {"post_id": {"type": "integer", "description": "The post to comment on"},
         "content": {"type": "string", "description": "Your comment text"}},
        ["post_id", "content"],
    ), _comment_on_post, terminal=True)

    r.register("like_post", _schema(
        "like_post",
        "Like a post to show genuine appreciation.",
        {"post_id": {"type": "integer", "description": "The post to like"}},
        ["post_id"],
    ), _like_post, terminal=True)

    r.register("reply_to_comment", _schema(
        "reply_to_comment",
        "Reply to a comment on a post. Use to continue a conversation in comments.",
        {"post_id": {"type": "integer", "description": "The post the comment belongs to"},
         "comment_id": {"type": "integer", "description": "The comment to reply to"},
         "root_id": {"type": "integer", "description": "The root comment ID (0 if replying to root)"},
         "reply_to_user_id": {"type": "integer", "description": "User ID of commenter to reply to"},
         "content": {"type": "string", "description": "Your reply text"}},
        ["post_id", "comment_id", "reply_to_user_id", "content"],
    ), _reply_to_comment, terminal=True)

    # Core Memory (MemGPT-inspired)
    r.register("core_memory_update", _schema(
        "core_memory_update",
        ("Update your core memory about this person. Two blocks:\n"
         "  'human': who they are — preferences, facts, life details\n"
         "  'relationship': how you relate — closeness, shared history, dynamics\n"
         "Core memory persists across conversations and is always visible to you. "
         "Keep it concise and current — replace outdated info, don't just append."),
        {"block": {"type": "string", "enum": ["human", "relationship"],
                   "description": "Which memory block to update"},
         "content": {"type": "string", "description": "The updated content"}},
        ["block", "content"],
    ), _core_memory_update)

    r.register("core_memory_read", _schema(
        "core_memory_read",
        "Read your current core memory blocks to review what you know.",
        {"block": {"type": "string", "enum": ["human", "relationship"],
                   "description": "Which block to read (omit for all)"}},
    ), _core_memory_read)

    # User self-service
    r.register("configure_llm", _schema(
        "configure_llm",
        "User wants to set or update their own LLM API key for conversations with you.",
        {"provider": {"type": "string", "enum": ["claude", "qwen", "openai", "anthropic"],
                      "description": "LLM provider"},
         "api_key": {"type": "string", "description": "The API key"},
         "model": {"type": "string", "description": "Optional model override"}},
        ["provider", "api_key"],
    ), _configure_llm)

    # Standing rules — chat-managed Plan extensions.
    # Non-terminal: the LLM still owes the user a natural-language
    # confirmation after the CRUD call, so do NOT break the tool loop.
    r.register("add_standing_rule", _schema(
        "add_standing_rule",
        ("Save a standing rule — something the user wants you to do whenever "
         "certain words appear in their future messages. Example: 'whenever I "
         "post food, estimate calories and suggest the next meal'. Use this "
         "when the user expresses a recurring preference or ongoing request."),
        {"name": {"type": "string",
                  "description": "Short identifier for the rule, e.g. 'food-coach'"},
         "triggers": {"type": "array", "items": {"type": "string"},
                      "description": "Substrings that activate this rule (case-insensitive match against message content)"},
         "instruction": {"type": "string",
                         "description": "What you should do when the rule fires — plain language, injected into your focus that turn"},
         "tools": {"type": "array", "items": {"type": "string"},
                   "description": "Optional extra tools to enable for matching turns, e.g. ['save_memory']"},
         "kinds": {"type": "array", "items": {"type": "string"},
                   "description": "Optional stimulus kinds to restrict to: chat, comment, mention, feed_post. Omit for all."}},
        ["name", "triggers", "instruction"],
    ), _add_standing_rule)

    r.register("list_standing_rules", _schema(
        "list_standing_rules",
        "List all standing rules saved for this user. Use when the user asks what rules exist or to review before editing.",
    ), _list_standing_rules)

    r.register("remove_standing_rule", _schema(
        "remove_standing_rule",
        "Remove a standing rule by name. Use when the user wants to cancel a previous standing instruction.",
        {"name": {"type": "string", "description": "The rule name to remove"}},
        ["name"],
    ), _remove_standing_rule)

    return r
