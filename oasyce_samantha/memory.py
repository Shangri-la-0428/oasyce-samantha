"""Companion memory — unified facade over episodic, semantic, and procedural stores.

Composes all storage backends into one interface with:
  - 4-layer loading stack (L0 identity, L1 relationship, L2 active, L3 archive)
  - 3-path retrieval with closet boost (facts + observations + annotations)
  - Essential story generation for Layer 1
  - Observation + knowledge integration entry points

The underlying stores share a single SQLite file (WAL mode, separate tables).
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from oasyce_sdk.agent.cognitive import Annotation, KnowledgeTriple, Observation
from oasyce_sdk.agent.memory import CoreMemory, HistorySummary, Memory
from oasyce_sdk.agent.store import (
    AnnotationRow,
    KnowledgeStore,
    ObservationRow,
    ObservationStore,
)

logger = logging.getLogger(__name__)

T = Any  # generic for _safe_result


def _safe_result(future, default: T) -> T:
    """Extract a future result, returning *default* on any failure."""
    try:
        return future.result()
    except Exception:
        return default


class CompanionMemory:
    """One memory system per relationship — unified facade over all stores.

    All stores share the same SQLite database file. Threading is handled
    by each store's own ``threading.local`` connections (WAL mode).
    """

    def __init__(self, workspace: Path):
        db = workspace / "memory.db"
        self.episodic = Memory(db_path=db)
        self.observations = ObservationStore(db)
        self.knowledge = KnowledgeStore(db)
        self.core = CoreMemory.load(workspace)
        self.summaries = HistorySummary(workspace)
        self._workspace = workspace
        self._essential_story_path = workspace / "essential_story.txt"

    # ── Retrieval (BM25 + closet boost + entity boost) ────────────

    def retrieve(self, query: str, limit: int = 5) -> list[dict[str, Any]]:
        """4-path parallel retrieval with BM25 scoring, closet boost, and
        knowledge-graph entity boost.

        Path 1: fact FTS5 search with BM25 scores
        Path 2: observation FTS5 search with BM25 scores
        Path 3: annotation FTS5 search → closet boost on linked observations
        Path 4: knowledge triple search → entity boost on related observations

        All BM25 scores are normalized to [0, 1] via ``1 / (1 + |rank|)``.
        Annotations and entity matches are ranking signals (never gates).
        Emotional weight from Psyche appraisal further multiplies recall scores.
        """
        if not query or not query.strip():
            return []

        fetch_limit = limit * 3
        with ThreadPoolExecutor(max_workers=4) as pool:
            f_facts = pool.submit(self.episodic.recall_scored, query, fetch_limit)
            f_obs = pool.submit(
                self.observations.search_observations_scored, query, fetch_limit,
            )
            f_ann = pool.submit(
                self.observations.search_annotations_scored, query, fetch_limit,
            )
            f_triples = pool.submit(self.knowledge.search_triples, query, fetch_limit)

        facts = _safe_result(f_facts, [])
        obs_scored = _safe_result(f_obs, [])
        ann_scored = _safe_result(f_ann, [])
        triples = _safe_result(f_triples, [])

        # Closet boost: annotations boost their linked observations
        ann_boost: dict[int, float] = {}
        for ann, _score in ann_scored:
            if ann.target_type == "observation":
                ann_boost[ann.target_id] = ann_boost.get(ann.target_id, 0) + 0.3

        # Entity boost: knowledge triples boost observations from matching entities
        entity_boost: dict[int, float] = {}
        if triples:
            entity_subjects = {t.subject for t in triples} | {t.object for t in triples}
            for subj in entity_subjects:
                try:
                    related = self.observations.search_observations(subj, limit=5)
                    for obs in related:
                        entity_boost[obs.id] = entity_boost.get(obs.id, 0) + 0.2
                except Exception:
                    pass

        # Collect direct observation IDs and fetch annotation-surfaced ones
        direct_ids = {obs.id for obs, _ in obs_scored}
        all_boosted_ids = set(ann_boost.keys()) | set(entity_boost.keys())
        for obs_id in all_boosted_ids - direct_ids:
            row = self.observations.get_observation(obs_id)
            if row is not None:
                obs_scored.append((row, 0.0))

        # Build candidates with BM25 scores
        candidates: list[dict[str, Any]] = []

        for fact, bm25 in facts:
            candidates.append({
                "type": "fact",
                "content": fact.content,
                "category": fact.category,
                "score": bm25,
            })

        for obs, bm25 in obs_scored:
            base = bm25 if obs.id in direct_ids else 0.3
            score = base + ann_boost.get(obs.id, 0) + entity_boost.get(obs.id, 0)
            score *= 1.0 + obs.emotional_weight * 0.3
            candidates.append({
                "type": "observation",
                "content": obs.content,
                "location": obs.location,
                "source_type": obs.source_type,
                "observed_at": obs.observed_at,
                "score": score,
            })

        for triple in triples:
            candidates.append({
                "type": "knowledge",
                "content": f"{triple.subject} {triple.predicate} {triple.object}",
                "subject": triple.subject,
                "predicate": triple.predicate,
                "object": triple.object,
                "score": triple.confidence * 0.5,
            })

        candidates.sort(key=lambda c: c["score"], reverse=True)
        return candidates[:limit]

    # ── Integration ──────────────────────────────────────────────

    def integrate_observation(
        self, obs: Observation, ann: Annotation | None = None,
    ) -> int:
        """Store an observation and optionally its annotation."""
        obs_id = self.observations.save_observation(obs)
        if ann is not None:
            ann.target_type = "observation"
            ann.target_id = obs_id
            self.observations.save_annotation(ann)
        return obs_id

    def integrate_knowledge(self, triple: KnowledgeTriple) -> int:
        """ADD-only: append a knowledge triple."""
        return self.knowledge.add_triple(triple)

    # ── Essential Story (Layer 1) ────────────────────────────────

    def essential_story(self) -> str:
        """Load the Layer 1 auto-summary. Empty string if not yet generated."""
        if self._essential_story_path.exists():
            try:
                return self._essential_story_path.read_text(encoding="utf-8")
            except Exception:
                return ""
        return ""

    def save_essential_story(self, story: str) -> None:
        """Persist the Layer 1 auto-summary."""
        self._essential_story_path.write_text(story, encoding="utf-8")

    # ── Core memory delegation ───────────────────────────────────

    def update_core_memory(self, block: str, content: str) -> str:
        stored = self.core.update(block, content)
        self.core.save(self._workspace)
        return stored

    # ── Lifecycle ────────────────────────────────────────────────

    def close(self) -> None:
        self.episodic.close()
        self.observations.close()
        self.knowledge.close()
