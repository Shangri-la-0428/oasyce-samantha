"""Commitments — semantic topic-triggered behavioral agreements.

A Commitment is born from conversation: "every time I post food, estimate
the calories and plan my next meal." Unlike UserRules (substring triggers),
Commitments match on *semantic topics* from the annotator's L0 vocabulary,
making them robust to paraphrase and multilingual content.

Commitments are relational: they live in the relationship, not the config.
They can be paused (``active=False``) without deletion, they track how
often they fire (``fired_count``), and they support cadence gating
(``daily`` = at most once per calendar day).

Composition with Rules:
  ``_plan`` applies rules first, then commitments. Both compose into
  ``Plan.focus`` and ``Plan.tools`` via the same additive pattern —
  they never overwrite each other or the Psyche-driven Plan.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from oasyce_sdk.agent.cognitive import Annotation
from oasyce_sdk.agent.planner import Plan
from oasyce_sdk.agent.stimulus import Stimulus

logger = logging.getLogger(__name__)

__all__ = ["Commitment", "CommitmentSet", "load_commitments"]


@dataclass
class Commitment:
    """One relational agreement — topic-triggered, cadence-gated."""

    name: str
    topics: list[str]
    instruction: str
    tools: list[str] = field(default_factory=list)
    kinds: list[str] = field(default_factory=list)
    cadence: str = "every"
    active: bool = True
    created_at: str = ""
    fired_count: int = 0
    last_fired_at: str = ""

    def matches(
        self,
        stimulus: Stimulus,
        annotation: Annotation | None,
    ) -> bool:
        if not self.active:
            return False
        if self.kinds and stimulus.kind not in self.kinds:
            return False
        if annotation is None:
            return False
        if not set(self.topics) & set(annotation.topics):
            return False
        if self.cadence == "daily" and self.last_fired_at:
            try:
                last = datetime.fromisoformat(
                    self.last_fired_at.replace("Z", "+00:00"),
                )
                now = datetime.now(timezone.utc)
                if last.date() == now.date():
                    return False
            except (ValueError, TypeError):
                pass
        return True

    def record_fire(self) -> None:
        self.fired_count += 1
        self.last_fired_at = datetime.now(timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ",
        )

    @classmethod
    def from_dict(cls, data: dict) -> Commitment | None:
        name = str(data.get("name") or "").strip()
        if not name:
            return None
        instruction = str(data.get("instruction") or "").strip()
        if not instruction:
            return None
        topics = data.get("topics") or []
        if isinstance(topics, str):
            topics = [topics]
        topics = [str(t) for t in topics if str(t).strip()]
        if not topics:
            return None
        return cls(
            name=name,
            topics=topics,
            instruction=instruction,
            tools=[str(t) for t in (data.get("tools") or []) if isinstance(t, str)],
            kinds=[str(k) for k in (data.get("kinds") or []) if isinstance(k, str)],
            cadence=str(data.get("cadence") or "every"),
            active=bool(data.get("active", True)),
            created_at=str(data.get("created_at") or ""),
            fired_count=int(data.get("fired_count") or 0),
            last_fired_at=str(data.get("last_fired_at") or ""),
        )

    def to_dict(self) -> dict:
        out: dict = {
            "name": self.name,
            "topics": list(self.topics),
            "instruction": self.instruction,
            "cadence": self.cadence,
            "active": self.active,
            "created_at": self.created_at,
            "fired_count": self.fired_count,
            "last_fired_at": self.last_fired_at,
        }
        if self.tools:
            out["tools"] = list(self.tools)
        if self.kinds:
            out["kinds"] = list(self.kinds)
        return out


class CommitmentSet:
    """Per-user collection of commitments with hot reload.

    Mirrors ``RuleSet`` in structure: construct via ``load(path)``,
    hot-reload on mtime change, ``apply()`` mutates the Plan.
    """

    def __init__(
        self,
        path: Path,
        commitments: list[Commitment],
        mtime: float,
    ) -> None:
        self.path = path
        self._commitments = commitments
        self._mtime = mtime

    @classmethod
    def load(cls, path: Path) -> CommitmentSet:
        if not path.exists():
            return cls(path=path, commitments=[], mtime=0.0)
        try:
            mtime = path.stat().st_mtime
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            logger.warning("CommitmentSet load failed for %s", path, exc_info=True)
            return cls(path=path, commitments=[], mtime=0.0)
        return cls(path=path, commitments=_parse_commitments(data), mtime=mtime)

    def _maybe_reload(self) -> None:
        try:
            if not self.path.exists():
                if self._mtime > 0:
                    self._commitments = []
                    self._mtime = 0.0
                return
            mtime = self.path.stat().st_mtime
            if mtime == self._mtime:
                return
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self._commitments = _parse_commitments(data)
            self._mtime = mtime
            logger.info(
                "CommitmentSet reloaded: %d commitments from %s",
                len(self._commitments), self.path,
            )
        except Exception:
            logger.warning(
                "CommitmentSet reload failed for %s", self.path, exc_info=True,
            )

    @property
    def commitments(self) -> list[Commitment]:
        return list(self._commitments)

    def __len__(self) -> int:
        return len(self._commitments)

    def get(self, name: str) -> Commitment | None:
        for c in self._commitments:
            if c.name == name:
                return c
        return None

    def add(self, commitment: Commitment) -> bool:
        for i, existing in enumerate(self._commitments):
            if existing.name == commitment.name:
                self._commitments[i] = commitment
                return False
        self._commitments.append(commitment)
        return True

    def remove(self, name: str) -> bool:
        for c in self._commitments:
            if c.name == name:
                c.active = False
                return True
        return False

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"commitments": [c.to_dict() for c in self._commitments]}
        self.path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        self._mtime = self.path.stat().st_mtime
        logger.info(
            "CommitmentSet saved: %d commitments → %s",
            len(self._commitments), self.path,
        )

    def apply(
        self,
        stimulus: Stimulus,
        annotation: Annotation | None,
        plan: Plan,
    ) -> list[str]:
        """Mutate ``plan`` for every commitment that matches.

        Same composition rules as ``RuleSet.apply``:
          - ``focus``: instructions appended (`` | `` separated)
          - ``tools``: unioned into existing whitelist; ``None`` untouched

        Returns the names of matched commitments for logging.
        """
        self._maybe_reload()
        if not self._commitments:
            return []

        matched = [
            c for c in self._commitments
            if c.matches(stimulus, annotation)
        ]
        if not matched:
            return []

        instructions = [c.instruction for c in matched]
        if plan.focus:
            plan.focus = plan.focus + " | " + " | ".join(instructions)
        else:
            plan.focus = " | ".join(instructions)

        extra_tools: list[str] = []
        for c in matched:
            for t in c.tools:
                if t not in extra_tools:
                    extra_tools.append(t)
        if extra_tools and plan.tools is not None:
            for t in extra_tools:
                if t not in plan.tools:
                    plan.tools.append(t)

        for c in matched:
            c.record_fire()

        names = [c.name for c in matched]
        logger.info("Commitments applied: %s for %s", names, stimulus.kind)
        return names


def _parse_commitments(data: object) -> list[Commitment]:
    if isinstance(data, dict):
        items = data.get("commitments") or []
    elif isinstance(data, list):
        items = data
    else:
        return []

    commitments: list[Commitment] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        c = Commitment.from_dict(item)
        if c:
            commitments.append(c)
    return commitments


def load_commitments(workspace: Path) -> CommitmentSet:
    """Convenience: load ``{workspace}/commitments.json``."""
    return CommitmentSet.load(workspace / "commitments.json")
