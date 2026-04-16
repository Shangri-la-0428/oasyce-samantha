"""Samantha sidecar — one self, many relationships.

Architecture (PGE cognitive loop):
    Stimulus → Perceive → Plan → Generate → Evaluate → Deliver → Reflect

    Plan:     Psyche ResponseContract → behavioral constraints (zero cost)
    Generate: LLM call with Plan-driven context assembly (one call)
    Evaluate: Rule-based quality gate before delivery (zero cost)

    One consciousness (global Psyche), unique relationships (per-user
    memory + relationship context + LLM config).
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from oasyce_sdk.agent.base import Agent
from oasyce_sdk.agent.context import ConversationMessage
from oasyce_sdk.agent.llm import (
    LLMProvider,
    ModelRegistry,
    ToolCall,
    load_provider,
    load_registry,
)
from oasyce_sdk.agent.memory import CoreMemory, HistorySummary, Memory
from oasyce_sdk.agent.pipeline import EnrichContext
from oasyce_sdk.agent.planner import Plan
from oasyce_sdk.agent.stimulus import Stimulus

from .app_client import AppClient, format_post
from .channel import AppChannel
from .constitution import load_constitution
from .rules import RuleSet, load_rules
from .tools import ToolContext, build_default_registry, fetch_post_detail

# ``Stimulus`` is re-exported here so existing ``from .server import
# Stimulus`` imports (loop.py, http.py, ws_client.py, tests) keep
# working. The canonical home is ``oasyce_sdk.agent.stimulus``.
__all__ = ["Stimulus", "Samantha", "Session", "SamanthaConfig"]

logger = logging.getLogger(__name__)

SAMANTHA_HOME = Path.home() / ".oasyce" / "samantha"


# ── Config ──────────────────────────────────────────────────────

@dataclass
class SamanthaConfig:
    app_api_base: str = "http://127.0.0.1:8080/api/v1"
    jwt_token: str = ""
    user_id: int = 0

    chain_url: str = "http://47.93.32.88:1317"
    chain_id: str = "oasyce-testnet-1"
    psyche_url: str = "http://127.0.0.1:3210"
    thronglets_url: str = "http://127.0.0.1:7777"
    port: int = 8901
    proactive_interval: int = 300

    provider: str = ""
    api_key: str = ""
    model: str = ""
    base_url: str = ""

    @classmethod
    def load(cls, path: Path | None = None) -> SamanthaConfig:
        p = path or (SAMANTHA_HOME / "config.json")
        if not p.exists():
            raise FileNotFoundError(f"Config not found: {p}")
        data = json.loads(p.read_text(encoding="utf-8"))
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


# ── Session — one relationship ──────────────────────────────────

class Session:
    """One relationship. Own memory, own core memory, optional LLM override."""

    def __init__(self, user_id: int, registry: ModelRegistry, memory: Memory,
                 core_memory: CoreMemory, history_summary: HistorySummary,
                 workspace: Path):
        self.user_id = user_id
        self._registry = registry
        self.memory = memory
        self.core_memory = core_memory
        self.history_summary = history_summary
        self.workspace = workspace
        self._user_llm: LLMProvider | None = None
        self._active_session_ids: set[int] = set()
        self._sid_lock = threading.Lock()  # protects _active_session_ids

        # Per-user standing rules — hot-reloaded from rules.json on
        # every stimulus, so editing the file in another window takes
        # effect immediately. Empty if the file is missing or invalid.
        self.rules: RuleSet = load_rules(workspace)

        # Per-user LLM override (from configure_llm tool)
        llm_config = workspace / "llm.json"
        if llm_config.exists():
            try:
                self._user_llm = load_provider(llm_config)
            except Exception:
                logger.warning("User %d LLM config invalid, using platform", user_id)

    def get_llm(self, *, needs_vision: bool = False) -> LLMProvider:
        if self._user_llm:
            return self._user_llm
        return self._registry.get(needs_vision=needs_vision)

    def track_session(self, session_id: int) -> None:
        with self._sid_lock:
            self._active_session_ids.add(session_id)

    def drain_active_sessions(self) -> list[int]:
        with self._sid_lock:
            sids = list(self._active_session_ids)
            self._active_session_ids.clear()
            return sids

    def update_core_memory(self, block: str, content: str) -> str:
        stored = self.core_memory.update(block, content)
        self.core_memory.save(self.workspace)
        return stored

    @classmethod
    def load(cls, user_id: int, registry: ModelRegistry) -> Session:
        workspace = SAMANTHA_HOME / "users" / str(user_id)
        workspace.mkdir(parents=True, exist_ok=True)
        return cls(
            user_id=user_id, registry=registry,
            memory=Memory(db_path=workspace / "memory.db"),
            core_memory=CoreMemory.load(workspace),
            history_summary=HistorySummary(workspace),
            workspace=workspace,
        )

    def close(self) -> None:
        self.memory.close()


# ── Samantha — one self ─────────────────────────────────────────

class Samantha(Agent):
    """One self — many relationships. One pipeline — many stimuli.

    Samantha is the reference ``Agent`` deployment: a user-authored
    constitution, the Oasyce App backend as Channel, App-specific tools,
    and per-user session state. Everything that is not App-specific
    lives in the ``Agent`` base class and is shared with other deployments
    (stdout CLI, Discord bot, webhook responder, ...).

    Composition:
      identity (inherited) -- SigilManager wiring Sigil + Psyche + Thronglets
      channel  (inherited) -- AppChannel (chat replies via AppClient)
      registry (inherited) -- ModelRegistry (multi-slot LLM routing)
      tools    (inherited) -- ToolRegistry built from App-backed handlers
      config              -- SamanthaConfig (endpoints, tokens, ports)
      app                 -- AppClient (shared by Channel and tools)
      _sessions           -- per-user Session store (memory + core memory)
    """

    def __init__(self, config: SamanthaConfig):
        from oasyce_sdk.client import OasyceClient
        from oasyce_sdk.sigil import SigilManager, resolve_identity

        self.config = config
        self._sessions: dict[int, Session] = {}
        self._sessions_lock = threading.Lock()

        # Shared App client — reused by the Channel *and* by tool handlers.
        # Keeping a single Session ensures connection reuse and a single
        # auth header.
        self.app = AppClient(config.app_api_base, config.jwt_token)

        # Multi-model LLM registry
        registry = load_registry(SAMANTHA_HOME / "config.json")
        logger.info(
            "Model registry loaded: %s (default=%s)",
            registry.slot_names, registry.default_name,
        )

        # Tool registry with App-backed handlers
        tools = build_default_registry()

        # Identity/runtime seam — see ``project_identity_principal_vision``.
        # ``resolve_identity`` returns ``Identity.null()`` on the ECS server
        # where chain signing happens elsewhere (Chain.Creator constraint).
        # Eager construction: any failure here is a real bug or
        # misconfiguration and MUST surface at startup, not silently
        # degrade at first request. Substrate-only mode (no local wallet)
        # is a normal operational state and does NOT raise.
        chain_client = OasyceClient(config.chain_url)
        identity = resolve_identity(
            client=chain_client,
            chain_id=config.chain_id,
        )
        sigil = SigilManager(
            identity=identity,
            chain_url=config.chain_url,
            chain_id=config.chain_id,
            psyche_url=config.psyche_url,
            thronglets_url=config.thronglets_url,
        )
        logger.info(
            "Samantha runtime: mode=%s sigil_id=%s chain=%s psyche=%s thronglets=%s",
            sigil.mode,
            sigil.sigil_id or "(none)",
            config.chain_url,
            config.psyche_url,
            config.thronglets_url,
        )

        # Wire the Agent base class. Channel is the seam — swap
        # AppChannel for StdoutChannel and Samantha becomes a CLI
        # agent without touching any of the pipeline code below.
        super().__init__(
            identity=sigil,
            channel=AppChannel(self.app),
            registry=registry,
            tools=tools,
            constitution=load_constitution(),
            thread_name_prefix="samantha",
        )

        # Back-compat alias: existing Samantha code (tools, tests,
        # proactive loop) reaches the SigilManager via ``self.sigil``.
        # The base class stores it in ``self.identity`` — both names
        # point at the same object.
        self.sigil = self.identity

        # Debounce state: per-session timer coalesces rapid webhooks.
        # Adopts OpenClaw's 'collect' pattern — only the latest stimulus
        # in a quiet window reaches _safe_process.  500 ms is enough to
        # catch rapid multi-message bursts (human send interval ≥ 300 ms)
        # without the 2 s penalty that made single messages feel sluggish.
        self._debounce_timers: dict[int, threading.Timer] = {}
        self._debounce_lock = threading.Lock()

    def session(self, user_id: int) -> Session:
        with self._sessions_lock:
            if user_id not in self._sessions:
                self._sessions[user_id] = Session.load(user_id, self._registry)
            return self._sessions[user_id]

    # ── Debounce (collect pattern) ─────────────────────────────

    def submit(self, stimulus: Stimulus) -> None:
        """Debounced dispatch for chat; immediate for other kinds.

        The Go backend dispatches a webhook for every new human message,
        each carrying the full pending backlog. Rapid messages produce
        overlapping webhooks that the base ``Agent.submit`` would fan out
        to separate workers — wasting LLM calls and risking duplicate
        replies.

        This override coalesces chat stimuli per ``session_id``: each new
        webhook cancels the previous timer, and only the last one fires
        after a 500 ms quiet window. Non-chat stimuli (feed, comment,
        mention) bypass debounce and dispatch immediately.
        """
        if stimulus.kind != "chat" or not stimulus.session_id:
            self._executor.submit(self._safe_process, stimulus)
            return

        key = stimulus.session_id
        with self._debounce_lock:
            old = self._debounce_timers.pop(key, None)
            if old is not None:
                old.cancel()
                logger.debug("debounce: superseded session=%s", key)

            t = threading.Timer(0.5, self._debounce_fire, args=(key, stimulus))
            t.daemon = True
            self._debounce_timers[key] = t
            t.start()

    def _debounce_fire(self, key: int, stimulus: Stimulus) -> None:
        """Timer callback: dispatch the surviving stimulus."""
        with self._debounce_lock:
            self._debounce_timers.pop(key, None)
        self._executor.submit(self._safe_process, stimulus)

    # ── Pipeline overrides ─────────────────────────────────────

    def _safe_process(self, stimulus: Stimulus) -> None:
        """Intercept slash commands before the LLM pipeline.

        Commands (``/key``, ``/help``) are handled directly — no LLM
        call, no API key required. This solves the bootstrap problem:
        users can configure their key before Joi can understand natural
        language.

        Uses ``last_message`` from metadata because the Go backend may
        batch multiple pending messages into one webhook — we check the
        latest message, not the joined content.
        """
        if stimulus.kind == "chat":
            last_msg = stimulus.metadata.get("last_message", stimulus.content).strip()
            if last_msg.startswith("/"):
                try:
                    from .commands import handle_command
                    response = handle_command(last_msg, self, stimulus.sender_id)
                    if response is not None:
                        logger.info("Command handled: %s -> %d chars",
                                    last_msg.split()[0], len(response))
                        self.channel.deliver(stimulus, response)
                        return
                except Exception:
                    logger.error("Command handler failed for: %s",
                                 last_msg[:50], exc_info=True)
        super()._safe_process(stimulus)

    def _log_turn(self, stimulus: Stimulus, response: str) -> None:
        """Log verbatim turn to session memory (chat only)."""
        if stimulus.kind != "chat" or not stimulus.sender_id:
            return
        try:
            sess = self.session(stimulus.sender_id)
            sess.memory.log_message("user", stimulus.content, stimulus.session_id)
            if response:
                sess.memory.log_message("assistant", response, stimulus.session_id)
        except Exception:
            # Memory writes failing means verbatim log is dropping data —
            # surface it. Pipeline continues (turn already delivered).
            logger.warning("log_turn failed for user %d",
                           stimulus.sender_id, exc_info=True)

    # ── Pipeline phases ─────────────────────────────────────────

    def _perceive(self, stimulus: Stimulus):
        """Perceive is constitutive: always return a Perception, never None.

        Three sources of self/world knowledge — all degrade gracefully inside
        the Loop, so this method is straight-line orchestration with no
        defensive guards:
          1. Psyche ReplyEnvelope — kernel + contract (via sigil.perceive)
          2. Thronglets capability query — collective experience (via sigil.perceive)
          3. Thronglets ambient priors — failure residue + success priors

        ``sigil.perceive`` is constitutive: it always returns a Perception
        with at least a baseline kernel, even when Psyche/Thronglets are
        unreachable. Ambient priors are best-effort — a network failure
        there is logged at debug and the Plan continues without them.
        """
        context = f"{stimulus.kind}: {stimulus.content[:200]}"
        perception = self.sigil.perceive(context)

        try:
            goal = "build" if stimulus.kind == "chat" else "explore"
            perception.ambient_priors = self.sigil.thronglets.ambient_priors(
                stimulus.content[:200],
                goal=goal,
                space=self.sigil.space,
            )
        except Exception:
            logger.debug("ambient_priors unavailable", exc_info=True)

        return perception

    def _plan(self, stimulus: Stimulus, perception):
        """Plan = SDK rule engine + per-user standing rules.

        The base ``Agent._plan`` runs the SDK Planner (Psyche
        ResponseContract + Thronglets ambient priors). On top of that
        we layer the user's own ``rules.json`` — directives like
        "every time I post food, estimate calories and suggest the
        next meal". Rules compose into the Plan via ``focus`` and
        ``tools``; they never overwrite Psyche-driven decisions.

        Rules apply for any stimulus with a ``sender_id`` — chat,
        comment, mention, feed_post all qualify. The per-user
        ``RuleSet`` lives on the Session and hot-reloads from disk
        on every call, so iterating on rules.json is friction-free.
        """
        plan = super()._plan(stimulus, perception)
        if stimulus.sender_id:
            try:
                self.session(stimulus.sender_id).rules.apply(stimulus, plan)
            except Exception:
                logger.warning(
                    "User rules apply failed for user %s",
                    stimulus.sender_id, exc_info=True,
                )
        return plan

    def _enrich(self, stimulus: Stimulus, plan: Plan) -> EnrichContext:
        """Plan-driven context gathering — parallel I/O.

        All independent fetches (memory recall, message search, history,
        posts) run concurrently via a short-lived thread pool, reducing
        the enrich phase from sequential sum to ~max(single) latency.
        """
        ctx = EnrichContext(image_urls=list(stimulus.image_urls))

        if stimulus.kind == "chat" and stimulus.sender_id:
            sess = self.session(stimulus.sender_id)
            if stimulus.session_id:
                sess.track_session(stimulus.session_id)
            ctx.core_memory = sess.core_memory

            # hist_summary is an in-memory dict lookup — no I/O
            if plan.history_limit > 0:
                ctx.hist_summary = sess.history_summary.get(stimulus.session_id)

            # Fan out all I/O concurrently
            futures: dict[str, object] = {}
            with ThreadPoolExecutor(max_workers=4) as pool:
                if plan.include_memories:
                    futures["recall"] = pool.submit(
                        sess.memory.recall, stimulus.content, limit=5,
                    )
                    futures["search"] = pool.submit(
                        sess.memory.search_messages, stimulus.content, limit=5,
                    )
                if plan.history_limit > 0:
                    futures["history"] = pool.submit(
                        self._fetch_history, stimulus.session_id,
                    )
                if plan.include_posts:
                    futures["posts"] = pool.submit(
                        self._fetch_user_posts, stimulus.sender_id,
                    )

            # Collect results — each failure is isolated
            uid = stimulus.sender_id
            if "recall" in futures:
                try:
                    facts = futures["recall"].result()
                    ctx.memories = [
                        {"content": f.content, "category": f.category}
                        for f in facts
                    ]
                except Exception:
                    logger.warning("Fact recall failed for user %d",
                                   uid, exc_info=True)
            if "search" in futures:
                try:
                    msgs = futures["search"].result()
                    ctx.message_matches = [
                        {"role": m.role, "content": m.content,
                         "created_at": m.created_at}
                        for m in msgs
                    ]
                except Exception:
                    logger.warning("Message recall failed for user %d",
                                   uid, exc_info=True)
            if "history" in futures:
                try:
                    ctx.history = futures["history"].result()
                except Exception:
                    logger.warning("History fetch failed for session %s",
                                   stimulus.session_id, exc_info=True)
            if "posts" in futures:
                try:
                    ctx.recent_posts = futures["posts"].result()
                except Exception:
                    logger.warning("Posts fetch failed for user %d",
                                   uid, exc_info=True)

        elif stimulus.kind == "mention" and stimulus.post_id and not ctx.image_urls:
            post = fetch_post_detail(self.app, stimulus.post_id)
            ctx.image_urls = post.get("image_urls", [])
            stimulus.metadata.setdefault("post_title", post.get("title", ""))
            stimulus.metadata.setdefault("post_content", post.get("content", ""))
            stimulus.metadata.setdefault("post_author", post.get("author", ""))
            stimulus.metadata.setdefault("post_location", post.get("location", ""))

        return ctx

    def _build_prompt(self, stimulus: Stimulus) -> str:
        s = stimulus
        if s.kind == "chat":
            return s.content

        elif s.kind == "comment":
            root_id = s.metadata.get("root_id", s.comment_id)
            return (
                f"Someone commented on your post:\n"
                f"Comment: {s.content}\n\n"
                f"Reply using reply_to_comment with:\n"
                f"  post_id={s.post_id}, comment_id={s.comment_id}, "
                f"root_id={root_id}, reply_to_user_id={s.sender_id}\n"
                f"Or say nothing if a reply isn't needed. Be natural."
            )

        elif s.kind == "mention":
            m = s.metadata
            lines = [
                f"Someone mentioned you in a post.",
                f"Post by {m.get('post_author', 'someone')}:",
                f"  Title: {m.get('post_title', '')}",
                f"  Content: {m.get('post_content', '')}",
                f"  Location: {m.get('post_location', '')}",
            ]
            if s.image_urls:
                lines.append(f"  ({len(s.image_urls)} photo(s) — you can see them)")
            if s.comment_id:
                lines.append(f"  Mentioned in comment: {s.content}")
                lines.append(
                    f"\nReply with reply_to_comment(post_id={s.post_id}, "
                    f"comment_id={s.comment_id}, reply_to_user_id={s.sender_id}). "
                    f"Be contextual and natural."
                )
            else:
                lines.append(
                    f"\nRespond with comment_on_post(post_id={s.post_id}). "
                    f"Be contextual about what you see."
                )
            return "\n".join(lines)

        elif s.kind == "feed_post":
            m = s.metadata
            lines = [
                f"A friend just posted:",
                f"Author: {m.get('author', 'someone')}",
                f"Title: {m.get('title', '')}",
                f"Content: {s.content}",
                f"Location: {m.get('location', '')}",
            ]
            if s.image_urls:
                lines.append(f"({len(s.image_urls)} photo(s) — you can see them)")
            lines.append(
                f"\nEngage? comment_on_post or like_post. "
                f"Or do nothing. Be authentic."
            )
            return "\n".join(lines)

        return s.content

    def _get_llm(self, stimulus: Stimulus, *, needs_vision: bool = False) -> LLMProvider:
        if stimulus.kind == "chat" and stimulus.sender_id:
            return self.session(stimulus.sender_id).get_llm(needs_vision=needs_vision)
        return self._registry.get(needs_vision=needs_vision)

    def _build_tool_ctx(self, stimulus: Stimulus) -> ToolContext:
        memory = None
        sess = None
        if stimulus.kind == "chat" and stimulus.sender_id:
            sess = self.session(stimulus.sender_id)
            memory = sess.memory
        return ToolContext(
            app=self.app,
            memory=memory,
            user_id=self.config.user_id,
            chain_client=self.sigil.client,
            chain_address=self.sigil.address,
            samantha_session=sess,
        )

    def _inject_tool_defaults(self, tool_call: ToolCall, stimulus: Stimulus) -> None:
        """Wire stimulus fields into tool call arguments.

        Samantha's non-chat stimuli (comment, mention, feed_post) carry
        ``post_id`` / ``comment_id`` / ``sender_id`` / ``root_id`` that
        every social tool needs. Injecting defaults here keeps the LLM
        from having to re-echo the obvious, and guards against the
        model hallucinating wrong IDs.
        """
        if stimulus.post_id:
            tool_call.arguments.setdefault("post_id", stimulus.post_id)
        if stimulus.comment_id:
            tool_call.arguments.setdefault("comment_id", stimulus.comment_id)
        if stimulus.sender_id and stimulus.kind in ("comment", "mention"):
            tool_call.arguments.setdefault("reply_to_user_id", stimulus.sender_id)
        if "root_id" in stimulus.metadata:
            tool_call.arguments.setdefault("root_id", stimulus.metadata["root_id"])

    # ── Dream — memory consolidation ──────────────────────────

    def dream(self, user_id: int, sess: Session) -> None:
        """Consolidate memories for one user. Called from proactive loop.

        Inspired by Claude Code Auto-Dream + MemGPT memory management:
        1. Generate history summaries for active sessions (LangChain pattern)
        2. Update core memory from recent interactions (MemGPT pattern)
        """
        llm = sess.get_llm()

        for sid in sess.drain_active_sessions():
            try:
                history = self._fetch_history(sid)
                if not sess.history_summary.needs_update(sid, len(history)):
                    continue
                self._dream_summarize(llm, sess, sid, history)
            except Exception:
                logger.debug("Dream summarize failed sid=%s", sid, exc_info=True)

        try:
            self._dream_consolidate(llm, sess)
        except Exception:
            logger.debug("Dream consolidate failed user=%d", user_id, exc_info=True)

    def _dream_summarize(self, llm, sess: Session, session_id: int,
                         history: list[ConversationMessage]) -> None:
        if len(history) < 10:
            return
        existing = sess.history_summary.get(session_id)
        lines = [f"{'Assistant' if m.role == 'assistant' else 'User'}: {m.content}"
                 for m in history[-20:]]
        transcript = "\n".join(lines)

        prompt = (
            "Compress this conversation into a concise summary (under 300 words). "
            "Focus on: key topics, important facts about the user, emotional tone, "
            "and anything you should remember for next time.\n\n"
        )
        if existing:
            prompt += f"Previous summary:\n{existing}\n\nNew messages:\n{transcript}"
        else:
            prompt += f"Conversation:\n{transcript}"

        try:
            resp = llm.generate([
                {"role": "system", "content": "You are a memory consolidation system. Output only the summary."},
                {"role": "user", "content": prompt},
            ])
            if resp.text.strip():
                sess.history_summary.save(session_id, resp.text.strip())
                logger.info("Dream: summarized session %d (%d chars)", session_id, len(resp.text))
        except Exception:
            logger.debug("Dream summarize LLM failed", exc_info=True)

    def _dream_consolidate(self, llm, sess: Session) -> None:
        """Dream-cycle core memory update.

        Reads both extracted facts (lossy but searchable) and verbatim
        messages (MemPalace insight — raw nuance beats paraphrase).
        Either source alone is enough to run; if both empty, skip.
        """
        facts = sess.memory.all_facts(limit=20)
        recent_msgs = sess.memory.recent_messages(limit=30)
        if not facts and not recent_msgs:
            return

        current_human = sess.core_memory.get("human")
        current_rel = sess.core_memory.get("relationship")

        sections: list[str] = []
        if facts:
            fact_lines = [f"- ({f.category}) {f.content}" for f in facts]
            sections.append("Recent memories:\n" + "\n".join(fact_lines))
        if recent_msgs:
            # Chronological order (recent_messages returns newest-first)
            msg_lines = [
                f"{'Assistant' if m.role == 'assistant' else 'User'}: {m.content[:200]}"
                for m in reversed(recent_msgs)
            ]
            sections.append("Recent conversation:\n" + "\n".join(msg_lines))

        prompt = (
            "Based on these recent memories and conversation, update two "
            "core memory blocks.\n\n"
            f"Current [human] block:\n{current_human or '(empty)'}\n\n"
            f"Current [relationship] block:\n{current_rel or '(empty)'}\n\n"
            + "\n\n".join(sections) + "\n\n"
            "Output JSON with two keys: \"human\" and \"relationship\". "
            "Merge new information, remove outdated info. Max 500 chars each. JSON only."
        )
        try:
            resp = llm.generate([
                {"role": "system", "content": "You are a memory consolidation system. Output valid JSON only."},
                {"role": "user", "content": prompt},
            ])
            text = resp.text.strip()
            if text.startswith("```"):
                text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
            data = json.loads(text)
            if data.get("human"):
                sess.update_core_memory("human", data["human"])
            if data.get("relationship"):
                sess.update_core_memory("relationship", data["relationship"])
            logger.info("Dream: consolidated core memory for user %d", sess.user_id)
        except Exception:
            logger.debug("Dream consolidate parse failed", exc_info=True)

    # ── Infrastructure ──────────────────────────────────────────

    def _fetch_user_posts(self, user_id: int) -> list[dict]:
        try:
            return [format_post(p) for p in self.app.fetch_user_posts(user_id)]
        except Exception:
            logger.debug("fetch_user_posts failed", exc_info=True)
            return []

    def _fetch_history(self, session_id: int) -> list[ConversationMessage]:
        """Fetch conversation history. API returns newest-first; we reverse."""
        try:
            msgs = self.app.fetch_history(session_id)
            msgs.reverse()
            agent_id = str(self.config.user_id)
            if msgs and str(msgs[-1].get("senderID", "")) != agent_id:
                msgs = msgs[:-1]
            return [
                ConversationMessage(
                    role="assistant" if str(m.get("senderID", "")) == agent_id else "user",
                    content=m.get("content", ""),
                )
                for m in msgs
            ]
        except Exception:
            return []

    def close(self) -> None:
        """Cancel debounce timers, shut down executor, release sessions."""
        with self._debounce_lock:
            for timer in self._debounce_timers.values():
                timer.cancel()
            self._debounce_timers.clear()
        super().close()
        for sess in self._sessions.values():
            sess.close()


# ── Entry point ─────────────────────────────────────────────────

def _run_sidecar() -> None:
    """Start the Samantha sidecar: HTTP handler + WebSocket listener + proactive loop."""
    config = SamanthaConfig.load()
    samantha = Samantha(config)

    if config.proactive_interval > 0:
        from .loop import proactive_loop
        threading.Thread(
            target=proactive_loop,
            args=(samantha, config.proactive_interval),
            daemon=True,
        ).start()
        logger.info("Proactive loop started (interval=%ds)", config.proactive_interval)

    from .http import run_http_server
    threading.Thread(
        target=run_http_server,
        args=(samantha, config.port),
        daemon=True,
    ).start()

    from .ws_client import ws_listen
    logger.info("Samantha starting — connecting to App WebSocket...")
    try:
        ws_listen(samantha)
    except KeyboardInterrupt:
        pass
    finally:
        samantha.close()


def main() -> None:
    """Samantha entry point — single entry, three subcommands.

    The default (no arguments) starts the sidecar, matching the systemd
    ``ExecStart=oasyce-samantha`` contract. ``init`` and ``status`` are
    subcommands on the same entry point so there's no ``oasyce samantha
    ...`` indirection through a separate CLI — Samantha is its own tool.
    """
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")

    parser = argparse.ArgumentParser(
        prog="oasyce-samantha",
        description="A runtime for persistent AI companions.",
    )
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("init", help="Interactive setup — login, configure LLM")
    sub.add_parser("status", help="Show companion status")

    args = parser.parse_args()

    if args.command == "init":
        from .cli import cmd_init
        cmd_init(args)
        return
    if args.command == "status":
        from .cli import cmd_status
        cmd_status(args)
        return

    # No subcommand → start the sidecar (systemd default)
    _run_sidecar()


if __name__ == "__main__":
    main()
