"""Phase 5 contract tests — Retrieval Enhancement.

Validates:
  - BM25 scored search methods (facts, observations, annotations)
  - Knowledge triple integration in retrieval
  - Entity boost from knowledge triples to observations
  - 4-path parallel retrieval with closet + entity boost
  - Score normalization and ranking correctness
"""

from __future__ import annotations

import pytest

from oasyce_sdk.agent.cognitive import Annotation, KnowledgeTriple, Observation


# ── BM25 scored search: facts ────────────────────────────────

class TestFactsScoredSearch:
    def test_recall_scored_returns_tuples(self, tmp_path):
        from oasyce_sdk.agent.memory import Memory

        mem = Memory(db_path=tmp_path / "test.db")
        mem.save("user enjoys hiking in snow mountains", "preference")
        mem.save("user favorite color is blue", "preference")

        results = mem.recall_scored("snow mountains")
        assert len(results) >= 1
        fact, score = results[0]
        assert "snow" in fact.content.lower()
        assert 0.0 < score <= 1.0

    def test_recall_scored_empty_query(self, tmp_path):
        from oasyce_sdk.agent.memory import Memory

        mem = Memory(db_path=tmp_path / "test.db")
        mem.save("something", "general")
        assert mem.recall_scored("") == []

    def test_recall_scored_updates_access_count(self, tmp_path):
        from oasyce_sdk.agent.memory import Memory

        mem = Memory(db_path=tmp_path / "test.db")
        fid = mem.save("snow mountain scenery", "preference")
        mem.recall_scored("snow mountain")
        facts = mem.all_facts()
        assert any(f.access_count >= 1 for f in facts)


# ── BM25 scored search: observations ─────────────────────────

class TestObservationsScoredSearch:
    def test_search_observations_scored(self, tmp_path):
        from oasyce_sdk.agent.store import ObservationStore

        store = ObservationStore(tmp_path / "test.db")
        store.save_observation(Observation(
            source_type="post", content="Beautiful Jade Dragon Snow Mountain photos",
        ))
        store.save_observation(Observation(
            source_type="post", content="Coffee shop in downtown area",
        ))

        results = store.search_observations_scored("snow mountain")
        assert len(results) >= 1
        obs, score = results[0]
        assert "Snow Mountain" in obs.content
        assert 0.0 < score <= 1.0

    def test_scored_results_ordered_by_relevance(self, tmp_path):
        from oasyce_sdk.agent.store import ObservationStore

        store = ObservationStore(tmp_path / "test.db")
        store.save_observation(Observation(
            source_type="post",
            content="Snow mountain hiking trail report with detailed snow conditions",
        ))
        store.save_observation(Observation(
            source_type="post",
            content="Regular weekend walk in the park near a small hill",
        ))

        results = store.search_observations_scored("snow mountain hiking")
        if len(results) >= 2:
            assert results[0][1] >= results[1][1]


# ── BM25 scored search: annotations ──────────────────────────

class TestAnnotationsScoredSearch:
    def test_search_annotations_scored(self, tmp_path):
        from oasyce_sdk.agent.store import ObservationStore

        store = ObservationStore(tmp_path / "test.db")
        obs_id = store.save_observation(Observation(
            source_type="post", content="Trip to Yunnan",
        ))
        store.save_annotation(Annotation(
            target_type="observation", target_id=obs_id,
            summary="Great snow mountain scenery in Lijiang area",
        ))

        results = store.search_annotations_scored("snow mountain")
        assert len(results) >= 1
        ann, score = results[0]
        assert "snow mountain" in ann.summary.lower()
        assert 0.0 < score <= 1.0


# ── BM25 normalization ───────────────────────────────────────

class TestBM25Normalization:
    def test_normalize_function(self):
        from oasyce_sdk.agent.store import _normalize_bm25

        assert _normalize_bm25(0.0) == pytest.approx(1.0)
        assert 0.0 < _normalize_bm25(-5.0) < 1.0
        assert 0.0 < _normalize_bm25(-100.0) < 0.1
        assert _normalize_bm25(-1.0) > _normalize_bm25(-10.0)


# ── Knowledge triple path in retrieval ───────────────────────

class TestKnowledgeTripleRetrieval:
    def test_triples_appear_in_results(self, tmp_path):
        from oasyce_samantha.memory import CompanionMemory

        mem = CompanionMemory(tmp_path)
        mem.knowledge.add_triple(KnowledgeTriple(
            subject="Jade Dragon Snow Mountain",
            predicate="located_in",
            object="Lijiang, Yunnan",
        ))

        results = mem.retrieve("Jade Dragon Snow Mountain")
        knowledge = [r for r in results if r["type"] == "knowledge"]
        assert len(knowledge) >= 1
        assert "Jade Dragon" in knowledge[0]["subject"]
        assert knowledge[0]["predicate"] == "located_in"
        mem.close()

    def test_triple_confidence_affects_score(self, tmp_path):
        from oasyce_samantha.memory import CompanionMemory

        mem = CompanionMemory(tmp_path)
        mem.knowledge.add_triple(KnowledgeTriple(
            subject="Snow Mountain",
            predicate="is_a",
            object="tourist attraction",
            confidence=0.9,
        ))

        results = mem.retrieve("Snow Mountain")
        knowledge = [r for r in results if r["type"] == "knowledge"]
        assert len(knowledge) >= 1
        assert knowledge[0]["score"] == pytest.approx(0.9 * 0.5)
        mem.close()


# ��─ Entity boost ─────────────────────────────────────────────

class TestEntityBoost:
    def test_triple_boosts_related_observations(self, tmp_path):
        from oasyce_samantha.memory import CompanionMemory

        mem = CompanionMemory(tmp_path)

        obs_id = mem.observations.save_observation(Observation(
            source_type="post",
            content="Visited Lijiang old town, amazing architecture",
            location="Lijiang",
        ))
        mem.knowledge.add_triple(KnowledgeTriple(
            subject="Jade Dragon Snow Mountain",
            predicate="located_in",
            object="Lijiang",
        ))

        results = mem.retrieve("Jade Dragon Snow Mountain", limit=10)
        obs = [r for r in results if r["type"] == "observation"]
        assert len(obs) >= 1
        assert "Lijiang" in obs[0]["content"]
        mem.close()

    def test_entity_boost_combines_with_annotation_boost(self, tmp_path):
        from oasyce_samantha.memory import CompanionMemory

        mem = CompanionMemory(tmp_path)

        obs_id = mem.observations.save_observation(Observation(
            source_type="post",
            content="Weekend trip around Lijiang area exploring local culture",
            location="Lijiang",
            emotional_weight=0.8,
        ))
        mem.observations.save_annotation(Annotation(
            target_type="observation", target_id=obs_id,
            summary="Travel experience near snow mountain region",
        ))
        mem.knowledge.add_triple(KnowledgeTriple(
            subject="Jade Dragon Snow Mountain",
            predicate="located_in",
            object="Lijiang",
        ))

        results = mem.retrieve("snow mountain", limit=10)
        obs = [r for r in results if r["type"] == "observation"]
        assert len(obs) >= 1
        assert obs[0]["score"] > 0.5
        mem.close()


# ── Full 4-path integration ──────────────────────────────────

class TestFourPathRetrieval:
    def test_all_types_in_results(self, tmp_path):
        from oasyce_samantha.memory import CompanionMemory

        mem = CompanionMemory(tmp_path)
        mem.episodic.save("User asked about snow mountain hiking trails", "topic")
        mem.observations.save_observation(Observation(
            source_type="post",
            content="Photos from Jade Dragon Snow Mountain summit",
        ))
        mem.knowledge.add_triple(KnowledgeTriple(
            subject="Snow Mountain",
            predicate="is_a",
            object="hiking destination",
        ))

        results = mem.retrieve("snow mountain", limit=10)
        types = {r["type"] for r in results}
        assert "fact" in types
        assert "observation" in types
        assert "knowledge" in types
        mem.close()

    def test_results_sorted_by_score_descending(self, tmp_path):
        from oasyce_samantha.memory import CompanionMemory

        mem = CompanionMemory(tmp_path)
        mem.episodic.save("snow mountain scenery is beautiful", "preference")
        mem.observations.save_observation(Observation(
            source_type="post",
            content="Detailed snow mountain hiking guide with photos",
            emotional_weight=0.9,
        ))

        results = mem.retrieve("snow mountain", limit=10)
        scores = [r["score"] for r in results]
        assert scores == sorted(scores, reverse=True)
        mem.close()

    def test_backward_compat_empty_query(self, tmp_path):
        from oasyce_samantha.memory import CompanionMemory

        mem = CompanionMemory(tmp_path)
        assert mem.retrieve("") == []
        assert mem.retrieve("   ") == []
        mem.close()

    def test_limit_respected(self, tmp_path):
        from oasyce_samantha.memory import CompanionMemory

        mem = CompanionMemory(tmp_path)
        for i in range(10):
            mem.episodic.save(f"snow mountain fact number {i}", "test")

        results = mem.retrieve("snow mountain", limit=3)
        assert len(results) <= 3
        mem.close()

    def test_graceful_when_stores_empty(self, tmp_path):
        from oasyce_samantha.memory import CompanionMemory

        mem = CompanionMemory(tmp_path)
        results = mem.retrieve("anything at all")
        assert results == []
        mem.close()
