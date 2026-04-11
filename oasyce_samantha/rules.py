"""User-defined standing rules — per-user Plan extensions.

Samantha's built-in Planner is a pure rule engine driven by the Psyche
ResponseContract and Thronglets ambient priors. It knows nothing about
*this particular user's* standing instructions: "every time I post food,
estimate the calories and suggest my next meal", "if I send a screenshot
of code, walk me through it line by line".

This module is the seam for those instructions. A ``RuleSet`` is loaded
from ``~/.oasyce/samantha/users/{id}/rules.json``. The Samantha agent
overrides ``Agent._plan`` to call ``rules.apply(stimulus, plan)`` after
the SDK Planner finishes, so the Psyche/Thronglets-driven Plan stays
intact and user rules layer on top — never replace it.

Why this is *not* a hack on ``focus``:

  ``Plan.focus`` is the right field to carry "what to pay attention to"
  hints into the prompt builder. It already exists for Psyche-driven
  caution focus and ambient-prior conflict focus. User rules slot into
  the same channel — they're another source of "what matters this turn",
  composed by concatenation rather than overwriting. The Generator
  doesn't need to know whether the focus came from a Thronglets prior
  or a user rule.

Why this is *not* a hack on ``tools``:

  ``Plan.tools`` is already an ``Optional[list[str]]`` where ``None``
  means "all tools available". User rules can name extra tools the LLM
  is allowed to use this turn (e.g. ``save_memory`` for a "remember
  what I just told you" rule), and ``RuleSet.apply`` does the union
  carefully — never narrowing an existing list, never silently
  overriding ``None``.

Hot reload:

  ``RuleSet`` checks the file's mtime on every ``apply`` and reloads
  if it changed. Editing ``rules.json`` in another window takes effect
  on the next stimulus — no Samantha restart needed. The cost is one
  ``Path.stat()`` per stimulus, which is well below the noise floor
  of an LLM call.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from oasyce_sdk.agent.planner import Plan
from oasyce_sdk.agent.stimulus import Stimulus

logger = logging.getLogger(__name__)

__all__ = ["UserRule", "RuleSet", "load_rules"]


# ── UserRule ────────────────────────────────────────────────────

@dataclass
class UserRule:
    """One standing instruction.

    Fields:
      name        : human-readable identifier (logs, future UI).
      triggers    : list of substrings matched case-insensitively
                    against ``stimulus.content``. Any hit triggers
                    the rule. Empty list = never match (rule is
                    effectively disabled).
      instruction : natural-language directive injected into
                    ``Plan.focus``. The Generator already wires
                    ``focus`` into the system prompt, so the LLM
                    sees this verbatim every time the rule fires.
      tools       : optional list of tool names to add to the
                    Plan's tool whitelist. Use this to enable
                    capabilities the default Planner would not
                    expose for this stimulus kind.
      kinds       : optional restriction to specific stimulus kinds
                    (chat/comment/mention/feed_post). Empty = all.
    """

    name: str
    triggers: list[str]
    instruction: str
    tools: list[str] = field(default_factory=list)
    kinds: list[str] = field(default_factory=list)

    def matches(self, stimulus: Stimulus) -> bool:
        if self.kinds and stimulus.kind not in self.kinds:
            return False
        if not self.triggers:
            return False
        text = stimulus.content.lower()
        return any(t.lower() in text for t in self.triggers)

    @classmethod
    def from_dict(cls, data: dict) -> "UserRule | None":
        """Best-effort parse. Returns ``None`` for malformed entries.

        We accept ``trigger`` (singular string), ``triggers`` (list),
        and a regex form ``trigger_regex`` for power users.
        """
        name = str(data.get("name") or "").strip()
        if not name:
            return None

        instruction = str(data.get("instruction") or "").strip()
        if not instruction:
            return None

        triggers: list[str] = []
        raw = data.get("triggers") or data.get("trigger")
        if isinstance(raw, str):
            triggers = [raw]
        elif isinstance(raw, list):
            triggers = [str(t) for t in raw if isinstance(t, (str, int))]

        # Optional regex form expanded into a single literal-friendly
        # pattern: callers wanting full regex can list multiple entries.
        regex = data.get("trigger_regex")
        if isinstance(regex, str) and regex:
            try:
                # Validate so a broken regex doesn't blow up at apply time.
                re.compile(regex, re.IGNORECASE)
                triggers.append(regex)
            except re.error as e:
                logger.warning("Invalid trigger_regex in rule %s: %s", name, e)

        tools_raw = data.get("tools") or []
        tools = [str(t) for t in tools_raw if isinstance(t, str)]

        kinds_raw = data.get("kinds") or []
        kinds = [str(k) for k in kinds_raw if isinstance(k, str)]

        return cls(
            name=name,
            triggers=triggers,
            instruction=instruction,
            tools=tools,
            kinds=kinds,
        )

    def to_dict(self) -> dict:
        """Serialise for round-trip persistence. Empty optional fields
        are omitted to keep the on-disk JSON tidy when humans inspect it.
        """
        out: dict = {
            "name": self.name,
            "triggers": list(self.triggers),
            "instruction": self.instruction,
        }
        if self.tools:
            out["tools"] = list(self.tools)
        if self.kinds:
            out["kinds"] = list(self.kinds)
        return out


# ── RuleSet ─────────────────────────────────────────────────────

class RuleSet:
    """Per-user collection of standing rules with hot reload.

    Construct via ``RuleSet.load(path)``; the path may not exist —
    a missing file is a valid empty RuleSet, not an error. Calling
    ``apply`` on an empty set is a no-op.
    """

    def __init__(self, path: Path, rules: list[UserRule], mtime: float) -> None:
        self.path = path
        self._rules = rules
        self._mtime = mtime

    @classmethod
    def load(cls, path: Path) -> "RuleSet":
        """Read ``path``. Missing or unreadable → empty RuleSet."""
        if not path.exists():
            return cls(path=path, rules=[], mtime=0.0)
        try:
            mtime = path.stat().st_mtime
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            logger.warning("RuleSet load failed for %s", path, exc_info=True)
            return cls(path=path, rules=[], mtime=0.0)

        return cls(path=path, rules=_parse_rules(data), mtime=mtime)

    def _maybe_reload(self) -> None:
        """Check mtime; reload if file changed since last read.

        Hot reload — editing rules.json in another window takes effect
        on the next stimulus, no Samantha restart needed.
        """
        try:
            if not self.path.exists():
                if self._rules:
                    self._rules = []
                    self._mtime = 0.0
                return
            mtime = self.path.stat().st_mtime
            if mtime == self._mtime:
                return
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self._rules = _parse_rules(data)
            self._mtime = mtime
            logger.info("RuleSet reloaded: %d rules from %s",
                        len(self._rules), self.path)
        except Exception:
            logger.warning("RuleSet reload failed for %s", self.path, exc_info=True)

    @property
    def rules(self) -> list[UserRule]:
        return list(self._rules)

    def __len__(self) -> int:
        return len(self._rules)

    def get(self, name: str) -> UserRule | None:
        for r in self._rules:
            if r.name == name:
                return r
        return None

    def add(self, rule: UserRule) -> bool:
        """Upsert by name. Returns True if a new rule was added,
        False if an existing rule was replaced.

        Upsert keeps the API to one method — chat-side natural-language
        flow doesn't need a separate ``update`` and the LLM doesn't have
        to ask "does this name already exist" before calling. Order is
        preserved on update so users see stable rule listings.
        """
        for i, existing in enumerate(self._rules):
            if existing.name == rule.name:
                self._rules[i] = rule
                return False
        self._rules.append(rule)
        return True

    def remove(self, name: str) -> bool:
        """Drop the rule with this name. Returns False if not found."""
        for i, r in enumerate(self._rules):
            if r.name == name:
                del self._rules[i]
                return True
        return False

    def save(self) -> None:
        """Write current rules back to ``self.path``.

        Creates parent directories if missing. Updates the cached mtime
        so the next ``apply()`` does not re-read the same content we
        just wrote (which would be a wasted parse, not a correctness
        issue, but worth avoiding).
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"rules": [r.to_dict() for r in self._rules]}
        self.path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        self._mtime = self.path.stat().st_mtime
        logger.info("RuleSet saved: %d rules → %s", len(self._rules), self.path)

    def apply(self, stimulus: Stimulus, plan: Plan) -> list[str]:
        """Mutate ``plan`` for every rule that matches ``stimulus``.

        Composition rules:
          - ``focus``: matched instructions are appended (separated by
            " | ") so Psyche/Thronglets focus is preserved.
          - ``tools``: matched tool names are unioned into the existing
            whitelist. ``None`` (=all) is left as None — never narrow.

        Returns the names of matched rules for logging.
        """
        self._maybe_reload()
        if not self._rules:
            return []

        matched: list[UserRule] = [r for r in self._rules if r.matches(stimulus)]
        if not matched:
            return []

        instructions = [r.instruction for r in matched]
        if plan.focus:
            plan.focus = plan.focus + " | " + " | ".join(instructions)
        else:
            plan.focus = " | ".join(instructions)

        extra_tools: list[str] = []
        for r in matched:
            for t in r.tools:
                if t not in extra_tools:
                    extra_tools.append(t)
        if extra_tools and plan.tools is not None:
            for t in extra_tools:
                if t not in plan.tools:
                    plan.tools.append(t)
        # If plan.tools is None, all tools are already available — no action.

        names = [r.name for r in matched]
        logger.info("Rules applied: %s for %s", names, stimulus.kind)
        return names


# ── Helpers ────────────────────────────────────────────────────

def _parse_rules(data: object) -> list[UserRule]:
    """Top-level parser. Accepts ``{"rules": [...]}`` or a bare list."""
    if isinstance(data, dict):
        items = data.get("rules") or []
    elif isinstance(data, list):
        items = data
    else:
        return []

    rules: list[UserRule] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        rule = UserRule.from_dict(item)
        if rule:
            rules.append(rule)
    return rules


def load_rules(workspace: Path) -> RuleSet:
    """Convenience: load ``{workspace}/rules.json`` as a RuleSet."""
    return RuleSet.load(workspace / "rules.json")
