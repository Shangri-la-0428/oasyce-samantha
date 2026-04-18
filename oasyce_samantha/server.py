"""Samantha runtime — one self, many relationships.

Architecture (PGE cognitive loop):
    Stimulus → Perceive → Plan → Generate → Evaluate → Deliver → Reflect

    Plan:     Psyche ResponseContract → behavioral constraints (zero cost)
    Generate: LLM call with Plan-driven context assembly (one call)
    Evaluate: Rule-based quality gate before delivery (zero cost)

    One consciousness (global Psyche), unique relationships (per-user
    memory + relationship context + LLM config), multiple surface
    adapters (local terminal, legacy App, future worlds).
"""

from __future__ import annotations

import argparse
import json
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from oasyce_sdk.agent.base import Agent
from oasyce_sdk.agent.cognitive import Annotation, Appraisal, CognitiveMode, Observation
from oasyce_sdk.agent.self import default_appraise

from .collective import (
    boost_corroborated,
    collect_annotations,
    is_already_shared,
    share_annotation,
)
from oasyce_sdk.agent.context import ConversationMessage
from oasyce_sdk.agent.llm import (
    LLMProvider,
    ToolCall,
    load_provider,
    load_registry,
)
from oasyce_sdk.agent.memory import CoreMemory, HistorySummary, Memory
from oasyce_sdk.agent.pipeline import EnrichContext
from oasyce_sdk.agent.planner import Plan
from oasyce_sdk.agent.stimulus import Stimulus

from .adapters import AdapterConfig, AdapterLoader
from .annotator import BatchAnnotator, annotate_level0
from .constitution import load_constitution
from .world import CompanionWorld
from .memory import CompanionMemory
from .rules import RuleSet, load_rules
from .tools import ToolContext, build_default_registry

# ``Stimulus`` is re-exported here so existing ``from .server import
# Stimulus`` imports (loop.py, http.py, ws_client.py, tests) keep
# working. The canonical home is ``oasyce_sdk.agent.stimulus``.
__all__ = ["Stimulus", "Samantha", "Session", "SamanthaConfig"]

logger = logging.getLogger(__name__)

SAMANTHA_HOME = Path.home() / ".oasyce" / "samantha"


# ── Config ──────────────────────────────────────────────────────

@dataclass
class SamanthaConfig:
    adapter: str = ""
    adapter_import: str = ""
    adapter_options: dict[str, Any] = field(default_factory=dict)

    app_api_base: str = ""
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
    local_user_id: int = 1
    local_session_id: int = 1

    @classmethod
    def load(cls, path: Path | None = None) -> SamanthaConfig:
        p = path or (SAMANTHA_HOME / "config.json")
        if not p.exists():
            raise FileNotFoundError(f"Config not found: {p}")
        data = json.loads(p.read_text(encoding="utf-8"))
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


class _NullRegistry:
    """Fallback registry used when Samantha starts without a platform LLM."""

    default_name = "(none)"
    slot_names: list[str] = []

    def get(self, *, needs_vision: bool = False):
        return None


# ── Session — one relationship ──────────────────────────────────

class Session:
    """One relationship. Own memory, own core memory, optional LLM override."""

    def __init__(self, user_id: int, registry, companion_memory: CompanionMemory,
                 workspace: Path):
        self.user_id = user_id
        self._registry = registry
        self._companion_memory = companion_memory
        self.workspace = workspace
        self._user_llm: LLMProvider | None = None
        self._active_session_ids: set[int] = set()
        self._sid_lock = threading.Lock()

        # Backward-compatible aliases — existing code accesses these directly
        self.memory = companion_memory.episodic
        self.core_memory = companion_memory.core
        self.history_summary = companion_memory.summaries
        self.observation_store = companion_memory.observations
        self.knowledge_store = companion_memory.knowledge

        self._turn_count: int = 0
        self._estimated_tokens: int = 0
        self._last_turn_time: float = 0.0

        self.rules: RuleSet = load_rules(workspace)

        llm_config = workspace / "llm.json"
        if llm_config.exists():
            try:
                self._user_llm = load_provider(llm_config)
            except Exception:
                logger.warning("User %d LLM config invalid, using platform", user_id)

    def get_llm(self, *, needs_vision: bool = False) -> LLMProvider | None:
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
        return self._companion_memory.update_core_memory(block, content)

    @classmethod
    def load(cls, user_id: int, registry) -> Session:
        workspace = SAMANTHA_HOME / "users" / str(user_id)
        workspace.mkdir(parents=True, exist_ok=True)
        return cls(
            user_id=user_id, registry=registry,
            companion_memory=CompanionMemory(workspace),
            workspace=workspace,
        )

    def record_turn(self, user_msg: str, response: str) -> None:
        import time as _time
        self._turn_count += 1
        self._estimated_tokens += (len(user_msg) + len(response)) // 2
        self._last_turn_time = _time.monotonic()

    def needs_flush(self, token_threshold: int = 80000, turn_threshold: int = 40) -> bool:
        return (
            self._estimated_tokens >= token_threshold
            or self._turn_count >= turn_threshold
        )

    def is_idle(self, timeout: float = 1800.0) -> bool:
        import time as _time
        if self._last_turn_time == 0.0:
            return False
        return (_time.monotonic() - self._last_turn_time) >= timeout

    def reset_counters(self) -> None:
        self._turn_count = 0
        self._estimated_tokens = 0

    def close(self) -> None:
        self._companion_memory.close()


# ── Samantha — one self ─────────────────────────────────────────

class Samantha(Agent):
    """One self — many relationships. One pipeline — many stimuli.

    Samantha is a companion runtime built on the generic ``Agent`` base:
    a user-authored constitution, per-user session state, proactive
    maintenance, and a deployment-level surface adapter. Everything that
    is not companion-specific stays in ``oasyce-sdk``.

    Composition:
      identity (inherited) -- SigilManager wiring Sigil + Psyche + Thronglets
      channel  (inherited) -- surface-provided delivery channel
      registry (inherited) -- ModelRegistry (multi-slot LLM routing)
      tools    (inherited) -- ToolRegistry built from core + adapter tools
      config              -- SamanthaConfig (runtime + adapter settings)
      surface_adapter     -- world integration for ingress/egress/tools
      _sessions           -- per-user Session store (memory + core memory)
    """

    def __init__(self, config: SamanthaConfig):
        from oasyce_sdk.client import OasyceClient
        from oasyce_sdk.sigil import SigilManager, resolve_identity

        self.config = config
        self._sessions: dict[int, Session] = {}
        self._sessions_lock = threading.Lock()
        self.adapter_config = AdapterConfig.from_runtime_config(config)
        self.surface_adapter = AdapterLoader.load(self.adapter_config)
        self.surface_capabilities = self.surface_adapter.capabilities
        self.app = getattr(self.surface_adapter, "app", None)  # back-compat alias

        # Multi-model LLM registry
        try:
            registry = load_registry(SAMANTHA_HOME / "config.json")
            logger.info(
                "Model registry loaded: %s (default=%s)",
                registry.slot_names, registry.default_name,
            )
        except Exception as e:
            registry = _NullRegistry()
            logger.warning(
                "Model registry unavailable: %s. Samantha will start command-first "
                "until an LLM is configured.",
                e,
            )

        # Companion-core tool registry with adapter-contributed tools.
        tools = build_default_registry()
        self.surface_adapter.contribute_tools(tools)

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

        # Wire the Agent base class. The surface adapter owns channel
        # construction so the companion core never needs to know which
        # world it is currently attached to.
        super().__init__(
            identity=sigil,
            channel=self.surface_adapter.make_channel(self),
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

        # Annotation cost tiers — background batch annotator (Level 1)
        def _default_obs_store():
            s = self.session(self.config.user_id or self.config.local_user_id or 1)
            return s.observation_store
        self._batch_annotator = BatchAnnotator(
            store=_default_obs_store(),
            get_llm=lambda: registry.get(),
        )

        logger.info(
            "Samantha surface adapter: %s caps=%s",
            self.surface_adapter.adapter_id,
            self.surface_capabilities,
        )

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

        Also stores feed posts as observations before pipeline runs —
        every post is persisted regardless of whether the Plan decides
        to observe or engage.
        """
        if stimulus.kind == "feed_post":
            obs_id = self._store_observation(stimulus)
            if obs_id is not None:
                stimulus.metadata["_obs_id"] = obs_id

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

        # PROACTIVE mode: skip pipeline, already has content to deliver
        super()._safe_process(stimulus)

    @property
    def _world(self):
        return CompanionWorld(self)

    def _deliver(self, stimulus: Stimulus, response: str) -> None:
        """Fallback for non-World callers. CompanionWorld handles mode routing."""
        self.channel.deliver(stimulus, response)

    def _store_observation(self, stimulus: Stimulus) -> int | None:
        """Persist a feed post as an Observation (OBSERVING mode).

        Returns obs_id for later enrichment with Psyche appraisal.
        Stored immediately with default emotional_weight (Write > Remember),
        enriched in _reflect when Perception is available.
        """
        user_id = stimulus.metadata.get("author_id") or stimulus.sender_id
        if not user_id:
            return None
        try:
            sess = self.session(self.config.user_id or self.config.local_user_id or 1)
            obs = Observation(
                source_type=stimulus.kind,
                source_id=stimulus.post_id,
                author_id=user_id,
                content=stimulus.content,
                media_urls=list(stimulus.image_urls),
                location=stimulus.metadata.get("location", ""),
            )
            obs_id = sess._companion_memory.integrate_observation(obs)
            if obs_id is not None:
                ann = annotate_level0(obs)
                if ann is not None:
                    ann.target_id = obs_id
                    sess.observation_store.save_annotation(ann)
                self._batch_annotator.enqueue(obs_id, obs)
            return obs_id
        except Exception:
            logger.debug("Store observation failed", exc_info=True)
            return None

    def _log_turn(self, stimulus: Stimulus, response: str) -> None:
        """Log verbatim turn to session memory (chat only)."""
        if stimulus.kind != "chat" or not stimulus.sender_id:
            return
        try:
            sess = self.session(stimulus.sender_id)
            sess.memory.log_message("user", stimulus.content, stimulus.session_id)
            if response:
                sess.memory.log_message("assistant", response, stimulus.session_id)
            sess.record_turn(stimulus.content, response or "")
            if sess.needs_flush():
                self._executor.submit(self._flush_session, stimulus.sender_id, sess)
        except Exception:
            logger.warning("log_turn failed for user %d",
                           stimulus.sender_id, exc_info=True)

    def _flush_session(self, user_id: int, sess: Session) -> None:
        """Lightweight pre-compaction: summarize + consolidate without full dream."""
        try:
            llm = sess.get_llm()
            if llm is None:
                return
            active_sessions = sess.drain_active_sessions()
            for sid in active_sessions:
                try:
                    self._dream_summarize_session(llm, sess, sid)
                except Exception:
                    logger.debug("Flush summarize failed for session %s", sid, exc_info=True)
            try:
                self._dream_consolidate(llm, sess)
            except Exception:
                logger.debug("Flush consolidate failed for user %d", user_id, exc_info=True)
            sess.reset_counters()
            logger.info("Pre-compaction flush complete for user %d", user_id)
        except Exception:
            logger.warning("Flush failed for user %d", user_id, exc_info=True)

    def _reflect(
        self,
        stimulus: Stimulus,
        response: str,
        perception,
    ) -> None:
        """Post-turn writeback: Psyche signals + observation enrichment.

        After the base reflect (Psyche writeback + Thronglets trace),
        enrich any stored observation with Psyche-derived emotional
        weight, and save a psyche snapshot for personality trajectory.
        """
        super()._reflect(stimulus, response, perception)

        kernel = getattr(perception, "kernel", None) if perception else None
        if kernel is None:
            return

        appraisal = self._appraise(stimulus, kernel)

        obs_id = stimulus.metadata.get("_obs_id")
        if obs_id is not None:
            try:
                sess = self.session(
                    self.config.user_id or self.config.local_user_id or 1,
                )
                snapshot = {
                    "vitality": kernel.vitality,
                    "tension": kernel.tension,
                    "warmth": kernel.warmth,
                    "guard": kernel.guard,
                }
                sess.observation_store.update_observation_appraisal(
                    obs_id, appraisal.emotional_weight, snapshot,
                )
            except Exception:
                logger.debug("Observation appraisal update failed", exc_info=True)

            self._share_observation_to_collective(stimulus, obs_id)

        if stimulus.kind == "chat" and stimulus.sender_id and appraisal.intensity > 0.6:
            self._save_psyche_snapshot(
                stimulus.sender_id, kernel, "high_intensity_turn",
                f"{stimulus.kind}: intensity={appraisal.intensity:.2f}",
            )

    @staticmethod
    def _appraise(stimulus: Stimulus, kernel) -> Appraisal:
        """Derive emotional encoding from Psyche kernel state."""
        return default_appraise(
            stimulus,
            vitality=kernel.vitality,
            tension=kernel.tension,
            warmth=kernel.warmth,
            guard=kernel.guard,
        )

    def _share_observation_to_collective(
        self, stimulus: Stimulus, obs_id: int,
    ) -> None:
        """Share observation annotation to Thronglets collective.

        Best-effort: dedup check + signal_post. Failures are silent.
        Only shares if the observation has annotations locally.
        """
        try:
            sess = self.session(
                self.config.user_id or self.config.local_user_id or 1,
            )
            anns = sess.observation_store.get_annotations_for("observation", obs_id)
            if not anns:
                return
            ann_row = anns[0]
            if is_already_shared(self.sigil, stimulus.post_id):
                return
            obs_row = sess.observation_store.get_observation(obs_id)
            if obs_row is None:
                return
            obs = Observation(
                source_type=obs_row.source_type,
                source_id=obs_row.source_id,
                content=obs_row.content,
            )
            ann = Annotation(
                topics=ann_row.topics,
                entities=ann_row.entities,
                summary=ann_row.summary,
            )
            share_annotation(self.sigil, obs, ann)
        except Exception:
            logger.debug("Collective share failed", exc_info=True)

    def _save_psyche_snapshot(
        self,
        user_id: int,
        kernel,
        trigger: str,
        session_summary: str = "",
    ) -> None:
        """Persist Psyche 4D state for personality trajectory tracking."""
        try:
            sess = self.session(user_id)
            sess.knowledge_store.save_psyche_snapshot(
                session_id=user_id,
                snapshot={
                    "vitality": kernel.vitality,
                    "tension": kernel.tension,
                    "warmth": kernel.warmth,
                    "guard": kernel.guard,
                    "trigger": trigger,
                    "session_summary": session_summary,
                },
            )
        except Exception:
            logger.debug("Psyche snapshot save failed", exc_info=True)

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

        Both calls run in parallel (~250ms each → ~250ms total instead
        of ~500ms serial).
        """
        context = f"{stimulus.kind}: {stimulus.content[:200]}"
        goal = "build" if stimulus.kind == "chat" else "explore"

        with ThreadPoolExecutor(max_workers=2) as pool:
            f_perception = pool.submit(self.sigil.perceive, context)
            f_priors = pool.submit(
                self.sigil.thronglets.ambient_priors,
                stimulus.content[:200],
                goal=goal,
                space=self.sigil.space,
            )

        perception = f_perception.result()

        try:
            perception.ambient_priors = f_priors.result()
            stimulus.metadata["_ambient_priors"] = perception.ambient_priors
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
        if stimulus.kind == "chat" and not self.surface_capabilities.chat:
            plan.intent = "observe"
            return plan
        if (
            stimulus.kind in {"comment", "mention", "feed_post"}
            and not self.surface_capabilities.social_feed
        ):
            plan.intent = "observe"
            return plan
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

            # Essential story (Layer 1, in-memory file read)
            ctx.essential_story = sess._companion_memory.essential_story()

            # Fan out all I/O concurrently
            futures: dict[str, object] = {}
            with ThreadPoolExecutor(max_workers=5) as pool:
                if plan.include_memories:
                    futures["recall"] = pool.submit(
                        sess.memory.recall, stimulus.content, limit=5,
                    )
                    futures["search"] = pool.submit(
                        sess.memory.search_messages, stimulus.content, limit=5,
                    )
                    futures["observations"] = pool.submit(
                        sess.observation_store.search_observations,
                        stimulus.content, 5,
                    )
                if plan.history_limit > 0:
                    futures["history"] = pool.submit(
                        self._fetch_history, stimulus,
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
            if "observations" in futures:
                try:
                    obs_rows = futures["observations"].result()
                    ctx.observations = [
                        {"content": o.content, "location": o.location,
                         "source_type": o.source_type, "observed_at": o.observed_at}
                        for o in obs_rows
                    ]
                except Exception:
                    logger.warning("Observation search failed for user %d",
                                   uid, exc_info=True)

        # Collective annotations (Thronglets cross-agent knowledge)
        priors = stimulus.metadata.get("_ambient_priors")
        if priors:
            collective = collect_annotations(priors)
            for ca in collective:
                ctx.observations.append({
                    "content": ca["summary"],
                    "source_type": "collective",
                    "location": "",
                    "observed_at": "",
                })

        try:
            self.surface_adapter.enrich(self, stimulus, plan, ctx)
        except Exception:
            logger.warning("Adapter enrich failed: %s", self.surface_adapter.adapter_id, exc_info=True)

        return ctx

    def _build_prompt(self, stimulus: Stimulus) -> str:
        prompt = self.surface_adapter.format_stimulus(stimulus)
        return prompt if prompt is not None else stimulus.content

    def _get_llm(
        self,
        stimulus: Stimulus,
        *,
        needs_vision: bool = False,
    ) -> LLMProvider | None:
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
            app=getattr(self.surface_adapter, "app", None),
            memory=memory,
            user_id=self.config.user_id,
            chain_client=self.sigil.client,
            chain_address=self.sigil.address,
            runtime=self,
            surface_adapter=self.surface_adapter,
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
        self.surface_adapter.inject_tool_defaults(tool_call, stimulus)

    # ── Dream — memory consolidation ──────────────────────────

    def dream(self, user_id: int, sess: Session) -> None:
        """Consolidate memories for one user. Called from proactive loop.

        Three-phase parallel dream cycle:
          Phase 1: Summarize all active sessions in parallel (LLM calls)
          Phase 2: Sequential consolidate + essential story (LLM, write order)
          Phase 3: Independent non-LLM tasks in parallel
        """
        llm = sess.get_llm()

        active_sessions = sess.drain_active_sessions()
        if active_sessions:
            with ThreadPoolExecutor(max_workers=4) as pool:
                futs = [
                    pool.submit(self._dream_summarize_session, llm, sess, sid)
                    for sid in active_sessions
                ]
                for f in futs:
                    try:
                        f.result()
                    except Exception:
                        pass

        try:
            self._dream_consolidate(llm, sess)
        except Exception:
            logger.debug("Dream consolidate failed user=%d", user_id, exc_info=True)

        try:
            self._dream_essential_story(llm, sess)
        except Exception:
            logger.debug("Dream essential story failed user=%d", user_id, exc_info=True)

        with ThreadPoolExecutor(max_workers=2) as pool:
            f_psyche = pool.submit(self._dream_psyche_snapshot, user_id)
            f_hebbian = pool.submit(self._dream_hebbian_boost, sess)
            try:
                f_psyche.result()
            except Exception:
                logger.debug("Dream psyche snapshot failed user=%d", user_id, exc_info=True)
            try:
                f_hebbian.result()
            except Exception:
                logger.debug("Dream hebbian boost failed user=%d", user_id, exc_info=True)

    def _dream_summarize_session(
        self, llm, sess: Session, sid: int,
    ) -> None:
        """Summarize one session — safe to call from ThreadPoolExecutor."""
        try:
            history = self._fetch_history(Stimulus(
                kind="chat", content="",
                sender_id=sess.user_id, session_id=sid,
            ))
            if not sess.history_summary.needs_update(sid, len(history)):
                return
            self._dream_summarize(llm, sess, sid, history)
        except Exception:
            logger.debug("Dream summarize failed sid=%s", sid, exc_info=True)

    def _dream_hebbian_boost(self, sess: Session) -> None:
        """Hebbian reinforcement: boost observations corroborated by collective."""
        try:
            priors = self.sigil.thronglets.ambient_priors(
                "observation annotation",
                space=self.sigil.space,
                limit=20,
            )
        except Exception:
            return
        recent = sess.observation_store.search_observations("", limit=50)
        local_ids = {o.source_id for o in recent if o.source_id}
        boosted = boost_corroborated(sess.observation_store, priors, local_ids)
        if boosted:
            logger.info("Hebbian boost: %d observations reinforced", boosted)

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

    def _dream_essential_story(self, llm, sess: Session) -> None:
        """Generate Layer 1 essential story from highest-weight memories.

        A ~500 token summary loaded on every wake-up (Layer 1) to give
        continuity without deep search. Regenerated each dream cycle.
        """
        facts = sess.memory.all_facts(limit=15)
        if not facts:
            return

        current = sess.core_memory.get("human")
        fact_lines = [f"- {f.content}" for f in facts]
        prompt = (
            "Write a brief story (~200 words) summarizing what you know about "
            "this person and your relationship. This will be loaded every time "
            "you wake up, so focus on the most important and emotionally "
            "meaningful things. Write in first person as if recalling memories.\n\n"
            f"Core memory about them:\n{current or '(nothing yet)'}\n\n"
            f"Recent memories:\n" + "\n".join(fact_lines)
        )
        try:
            resp = llm.generate([
                {"role": "system", "content": "You are a memory consolidation system. Output only the story."},
                {"role": "user", "content": prompt},
            ])
            if resp.text.strip():
                sess._companion_memory.save_essential_story(resp.text.strip())
                logger.info("Dream: essential story updated for user %d", sess.user_id)
        except Exception:
            logger.debug("Dream essential story LLM failed", exc_info=True)

    def _dream_psyche_snapshot(self, user_id: int) -> None:
        """Save Psyche state at session boundary for personality trajectory."""
        kernel = getattr(self.sigil, "_last_kernel", None)
        if kernel is None:
            try:
                context = f"session_boundary: user {user_id}"
                perception = self.sigil.perceive(context)
                kernel = perception.kernel
            except Exception:
                logger.debug("Cannot get kernel for psyche snapshot", exc_info=True)
                return
        self._save_psyche_snapshot(
            user_id, kernel, "session_boundary", "dream cycle",
        )

    # ── Infrastructure ──────────────────────────────────────────

    def _fetch_user_posts(self, user_id: int) -> list[dict]:
        fetcher = getattr(self.surface_adapter, "fetch_user_posts", None)
        if not callable(fetcher):
            return []
        try:
            return fetcher(self, user_id)
        except Exception:
            logger.debug("fetch_user_posts failed", exc_info=True)
            return []

    def _fetch_history(self, stimulus: Stimulus) -> list[ConversationMessage]:
        """Fetch conversation history from the active surface, or local memory."""
        fetcher = getattr(self.surface_adapter, "fetch_history", None)
        if callable(fetcher):
            try:
                return fetcher(self, stimulus)
            except Exception:
                logger.debug("adapter fetch_history failed", exc_info=True)
        if stimulus.kind != "chat" or not stimulus.sender_id:
            return []
        try:
            session = self.session(stimulus.sender_id)
            msgs = session.memory.recent_messages(
                session_id=stimulus.session_id or 0,
                limit=20,
            )
            msgs.reverse()
            return [
                ConversationMessage(
                    role=m.role,
                    content=m.content,
                )
                for m in msgs
            ]
        except Exception:
            return []

    def scan_proactive_inputs(self, seen_posts: set[int], seen_comments: set[int]) -> None:
        scanner = getattr(self.surface_adapter, "scan_proactive_inputs", None)
        if callable(scanner):
            scanner(self, seen_posts, seen_comments)

    def deliver_proactive(
        self,
        user_id: int,
        content: str,
        *,
        urgency: float = 0.3,
        context: dict | None = None,
    ) -> bool:
        if not content:
            return False

        deliverer = getattr(self.surface_adapter, "deliver_proactive", None)
        if callable(deliverer):
            return bool(deliverer(self, user_id, content, urgency, context))

        if not self.surface_capabilities.chat:
            return False

        session_id = (context or {}).get("session_id") or self.config.local_session_id or user_id
        self.channel.deliver(
            Stimulus(
                kind="chat",
                content="",
                sender_id=user_id,
                session_id=session_id,
            ),
            content,
        )
        return True

    def close(self) -> None:
        """Cancel debounce timers, shut down executor, release sessions."""
        with self._debounce_lock:
            for timer in self._debounce_timers.values():
                timer.cancel()
            self._debounce_timers.clear()
        self._batch_annotator.stop()
        try:
            self.surface_adapter.stop()
        except Exception:
            logger.debug("adapter stop failed", exc_info=True)
        super().close()
        for sess in self._sessions.values():
            sess.close()


# ── Entry point ─────────────────────────────────────────────────

def _run_sidecar() -> None:
    """Start Samantha using the configured surface adapter."""
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

    logger.info("Samantha starting on adapter=%s", samantha.surface_adapter.adapter_id)
    try:
        samantha.surface_adapter.start(samantha)
    except KeyboardInterrupt:
        pass
    finally:
        samantha.close()


def main() -> None:
    """Samantha entry point — single entry, three subcommands.

    The default (no arguments) starts the runtime, matching the systemd
    ``ExecStart=oasyce-samantha`` contract. ``init`` and ``status`` are
    subcommands on the same entry point so there's no ``oasyce samantha
    ...`` indirection through a separate CLI — Samantha is its own tool.
    """
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")

    parser = argparse.ArgumentParser(
        prog="oasyce-samantha",
        description="An AI companion runtime that can run locally or attach to a surface.",
    )
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("init", help="Interactive setup — choose surface, configure LLM")
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

    # No subcommand → start the configured runtime (systemd default)
    _run_sidecar()


if __name__ == "__main__":
    main()
