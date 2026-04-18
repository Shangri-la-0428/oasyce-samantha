"""Collective knowledge — Thronglets-mediated annotation sharing.

When Samantha annotates an observation, the annotation is shared to
the collective via Thronglets signal_post. Other agents receiving
ambient_priors for similar contexts get cross-agent annotations
without redundant LLM calls.

Protocol:
  share   → signal_post(kind="info", context=JSON{type, source_id, ...})
  receive → ambient_priors → parse signals with type="observation_annotation"
  dedup   → check signal_feed before sharing duplicate annotations
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from oasyce_sdk.agent.cognitive import Annotation, Observation

if TYPE_CHECKING:
    from oasyce_sdk.sigil import SigilManager

logger = logging.getLogger(__name__)


def share_annotation(
    sigil: "SigilManager",
    obs: Observation,
    ann: Annotation,
) -> bool:
    """Post an observation annotation to the collective.

    Returns True if the signal was accepted, False on failure.
    Failures are silent — collective sharing is best-effort and
    must never block the local pipeline.
    """
    if not ann.summary:
        return False
    try:
        context = json.dumps({
            "type": "observation_annotation",
            "source_id": obs.source_id,
            "source_type": obs.source_type,
            "topics": ann.topics,
            "entities": ann.entities,
        }, ensure_ascii=False)
        result = sigil.thronglets.signal_post(
            context=context,
            kind="info",
            message=ann.summary[:200],
            space=sigil.space,
        )
        return bool(result)
    except Exception:
        logger.debug("share_annotation failed", exc_info=True)
        return False


def collect_annotations(priors: dict | None) -> list[dict[str, Any]]:
    """Extract cross-agent annotation signals from ambient_priors.

    Parses the priors payload for signals whose context contains
    ``type: "observation_annotation"``. Returns a list of dicts with
    topics, entities, summary, and confidence.
    """
    if not priors or not isinstance(priors, dict):
        return []
    results: list[dict[str, Any]] = []
    for item in priors.get("priors") or []:
        if not isinstance(item, dict):
            continue
        try:
            ctx_raw = item.get("context", "")
            if not ctx_raw:
                continue
            ctx = json.loads(ctx_raw) if isinstance(ctx_raw, str) else ctx_raw
            if not isinstance(ctx, dict):
                continue
            if ctx.get("type") != "observation_annotation":
                continue
            results.append({
                "topics": ctx.get("topics", []),
                "entities": ctx.get("entities", []),
                "summary": item.get("summary") or item.get("message", ""),
                "confidence": float(item.get("confidence", 0.5)),
                "source_id": ctx.get("source_id", 0),
            })
        except (json.JSONDecodeError, TypeError, ValueError):
            continue
    return results


def is_already_shared(
    sigil: "SigilManager",
    source_id: int,
) -> bool:
    """Check if an annotation for this source already exists in the collective.

    Uses signal_feed to look for recent annotation signals matching
    the source_id. Best-effort — returns False on any failure.
    """
    if not source_id:
        return False
    try:
        signals = sigil.thronglets.signal_feed(
            hours=72,
            kind="info",
            limit=20,
        )
        for sig in signals:
            try:
                ctx = json.loads(sig.context) if sig.context else {}
                if (
                    ctx.get("type") == "observation_annotation"
                    and ctx.get("source_id") == source_id
                ):
                    return True
            except (json.JSONDecodeError, TypeError):
                continue
    except Exception:
        logger.debug("is_already_shared check failed", exc_info=True)
    return False


def boost_corroborated(
    observation_store,
    priors: dict | None,
    local_source_ids: set[int],
) -> int:
    """Hebbian reinforcement: boost observations corroborated by collective.

    When a collective annotation references a source_id that we also
    have locally, bump the observation's emotional_weight by 0.1
    (capped at 1.0). Returns the number of boosted observations.
    """
    annotations = collect_annotations(priors)
    if not annotations:
        return 0
    boosted = 0
    for ann in annotations:
        sid = ann.get("source_id", 0)
        if sid and sid in local_source_ids:
            try:
                obs = observation_store.get_observation_by_source_id(sid)
                if obs is None:
                    continue
                new_weight = min(1.0, obs.emotional_weight + 0.1)
                if new_weight > obs.emotional_weight:
                    observation_store.update_observation_appraisal(
                        obs.id, new_weight, obs.psyche_snapshot,
                    )
                    boosted += 1
            except Exception:
                logger.debug("boost_corroborated failed for %d", sid, exc_info=True)
    return boosted
