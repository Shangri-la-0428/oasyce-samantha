"""Tool system — Samantha's core tool handlers and registry builder.

Imports generic ``Tool``, ``ToolRegistry`` and ``schema`` from
``oasyce_sdk.agent.tools``; extends ``ToolContext`` with Samantha
runtime references; registers the companion-core memory/economic/rules
handlers that are valid no matter which surface Samantha is connected to.

Why the split between ``oasyce_sdk.agent.tools`` and this module:

- ``oasyce_sdk.agent.tools`` owns the *mechanism* — registry, dispatch,
  schema helper, base ``ToolContext``. It has no imports from any
  deployment so the agent pipeline can depend on it without cycles.

- Surface-specific toolpacks live with their adapter implementations
  (for example the legacy Oasyce App social tools).
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

__all__ = [
    "Tool",
    "ToolContext",
    "ToolRegistry",
    "build_default_registry",
]

logger = logging.getLogger(__name__)


# ── Tool context (Samantha-specific extension) ─────────────────

@dataclass
class ToolContext(_BaseToolContext):
    """Per-stimulus tool bundle for Samantha.

    Extends the generic ``oasyce_sdk.agent.tools.ToolContext`` with
    Samantha runtime references. Surface adapters can stash transport
    handles on the context (for example ``app`` on the legacy adapter),
    but the core registry itself does not depend on any concrete world.
    """
    app: Any = None
    runtime: Any = None
    surface_adapter: Any = None
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
    if ctx.samantha_session is None:
        return json.dumps({"error": "no session — use /key set command instead"})

    workspace = ctx.samantha_session.workspace
    llm_cfg: dict[str, str] = {"provider": args["provider"], "api_key": args["api_key"]}
    if args.get("model"):
        llm_cfg["model"] = args["model"]
    if args.get("base_url"):
        llm_cfg["base_url"] = args["base_url"]

    llm_path = workspace / "llm.json"
    llm_path.write_text(json.dumps(llm_cfg, indent=2), encoding="utf-8")

    # Hot-reload so the next message uses the new key immediately
    try:
        from oasyce_sdk.agent.llm import load_provider
        ctx.samantha_session._user_llm = load_provider(llm_path)
    except Exception:
        llm_path.unlink(missing_ok=True)
        ctx.samantha_session._user_llm = None
        return json.dumps({"error": "config written but validation failed — reverted"})

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
        {"provider": {"type": "string",
                      "enum": ["openai", "anthropic", "claude", "xai",
                               "kimi", "deepseek", "qwen", "gemini"],
                      "description": "LLM provider"},
         "api_key": {"type": "string", "description": "The API key"},
         "model": {"type": "string", "description": "Optional model override"},
         "base_url": {"type": "string", "description": "Optional custom API endpoint"}},
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
