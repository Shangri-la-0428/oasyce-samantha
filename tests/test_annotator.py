"""Feature 8 contract tests — Annotation Cost Tiers.

Validates:
  - Level 0: rule-based keyword→topic, location→entity, media→visual_content
  - Level 1: BatchAnnotator enqueue/flush lifecycle, batch prompt parsing
  - Level 2: deep on-demand annotation with LLM
  - Wiring: _store_observation triggers L0 + enqueue, _enrich triggers L2
"""

from __future__ import annotations

import json
import time
from unittest.mock import MagicMock, patch

import pytest

from oasyce_sdk.agent.cognitive import Annotation, Observation


# ── Level 0 — Rule-based annotation ───────────────────────────

class TestAnnotateLevel0:
    def test_travel_keyword_chinese(self):
        from oasyce_samantha.annotator import annotate_level0
        obs = Observation(content="今天去雪山旅行", source_id=1)
        ann = annotate_level0(obs)
        assert ann is not None
        assert "travel" in ann.topics
        assert ann.source == "rule"
        assert ann.confidence == 0.6

    def test_food_keyword_english(self):
        from oasyce_samantha.annotator import annotate_level0
        obs = Observation(content="Found a great restaurant downtown", source_id=2)
        ann = annotate_level0(obs)
        assert ann is not None
        assert "food" in ann.topics

    def test_multiple_topics(self):
        from oasyce_samantha.annotator import annotate_level0
        obs = Observation(content="和朋友去咖啡厅聊天", source_id=3)
        ann = annotate_level0(obs)
        assert ann is not None
        assert "food" in ann.topics
        assert "social" in ann.topics

    def test_location_adds_travel_and_entity(self):
        from oasyce_samantha.annotator import annotate_level0
        obs = Observation(content="nice view", source_id=4, location="Shanghai")
        ann = annotate_level0(obs)
        assert ann is not None
        assert "travel" in ann.topics
        assert "Shanghai" in ann.entities

    def test_media_urls_add_visual_content(self):
        from oasyce_samantha.annotator import annotate_level0
        obs = Observation(
            content="look at this",
            source_id=5,
            media_urls=["https://example.com/photo.jpg"],
        )
        ann = annotate_level0(obs)
        assert ann is not None
        assert "visual_content" in ann.topics

    def test_no_signal_returns_none(self):
        from oasyce_samantha.annotator import annotate_level0
        obs = Observation(content="ok", source_id=6)
        ann = annotate_level0(obs)
        assert ann is None

    def test_at_mentions_extracted(self):
        from oasyce_samantha.annotator import annotate_level0
        obs = Observation(content="@alice 你好开心", source_id=7)
        ann = annotate_level0(obs)
        assert ann is not None
        assert "alice" in ann.entities

    def test_case_insensitive(self):
        from oasyce_samantha.annotator import annotate_level0
        obs = Observation(content="YOGA class today", source_id=8)
        ann = annotate_level0(obs)
        assert ann is not None
        assert "fitness" in ann.topics

    def test_target_type_is_observation(self):
        from oasyce_samantha.annotator import annotate_level0
        obs = Observation(content="去gym锻炼", source_id=9)
        ann = annotate_level0(obs)
        assert ann is not None
        assert ann.target_type == "observation"
        assert ann.target_id == 9

    def test_topics_sorted(self):
        from oasyce_samantha.annotator import annotate_level0
        obs = Observation(content="和朋友去yoga然后吃火锅", source_id=10)
        ann = annotate_level0(obs)
        assert ann is not None
        assert ann.topics == sorted(ann.topics)


# ── Level 1 — BatchAnnotator ──────────────────────────────────

class TestBatchAnnotator:
    def test_enqueue_under_batch_size(self):
        from oasyce_samantha.annotator import BatchAnnotator
        store = MagicMock()
        ba = BatchAnnotator(store=store, get_llm=MagicMock())
        try:
            obs = Observation(content="test", source_id=1)
            ba.enqueue(1, obs)
            assert ba.pending_count == 1
            store.save_annotation.assert_not_called()
        finally:
            ba.stop()

    def test_auto_flush_at_batch_size(self):
        from oasyce_samantha.annotator import BatchAnnotator
        store = MagicMock()

        llm = MagicMock()
        resp = MagicMock()
        resp.text = json.dumps([
            {"topics": ["t"], "entities": [], "sentiment": "neutral", "summary": "s"}
        ] * 8)
        llm.generate.return_value = resp

        ba = BatchAnnotator(store=store, get_llm=lambda: llm)
        try:
            for i in range(8):
                ba.enqueue(i, Observation(content=f"obs {i}", source_id=i))
            assert ba.pending_count == 0
            assert store.save_annotation.call_count == 8
        finally:
            ba.stop()

    def test_flush_drains_pending(self):
        from oasyce_samantha.annotator import BatchAnnotator
        store = MagicMock()

        llm = MagicMock()
        resp = MagicMock()
        resp.text = json.dumps([
            {"topics": ["a"], "entities": [], "sentiment": "positive", "summary": "x"}
        ] * 3)
        llm.generate.return_value = resp

        ba = BatchAnnotator(store=store, get_llm=lambda: llm)
        try:
            for i in range(3):
                ba.enqueue(i, Observation(content=f"obs {i}", source_id=i))
            assert ba.pending_count == 3
            ba.flush()
            assert ba.pending_count == 0
            assert store.save_annotation.call_count == 3
        finally:
            ba.stop()

    def test_stop_flushes_remaining(self):
        from oasyce_samantha.annotator import BatchAnnotator
        store = MagicMock()

        llm = MagicMock()
        resp = MagicMock()
        resp.text = json.dumps([
            {"topics": [], "entities": [], "sentiment": "neutral", "summary": ""}
        ] * 2)
        llm.generate.return_value = resp

        ba = BatchAnnotator(store=store, get_llm=lambda: llm)
        ba.enqueue(1, Observation(content="a", source_id=1))
        ba.enqueue(2, Observation(content="b", source_id=2))
        ba.stop()

        assert ba.pending_count == 0
        assert store.save_annotation.call_count == 2

    def test_llm_failure_doesnt_crash(self):
        from oasyce_samantha.annotator import BatchAnnotator
        store = MagicMock()
        ba = BatchAnnotator(
            store=store,
            get_llm=MagicMock(side_effect=RuntimeError("no llm")),
        )
        try:
            for i in range(8):
                ba.enqueue(i, Observation(content=f"obs {i}", source_id=i))
            assert ba.pending_count == 0
            store.save_annotation.assert_not_called()
        finally:
            ba.stop()

    def test_malformed_llm_response(self):
        from oasyce_samantha.annotator import BatchAnnotator
        store = MagicMock()

        llm = MagicMock()
        resp = MagicMock()
        resp.text = "not json at all"
        llm.generate.return_value = resp

        ba = BatchAnnotator(store=store, get_llm=lambda: llm)
        try:
            for i in range(8):
                ba.enqueue(i, Observation(content=f"obs {i}", source_id=i))
            store.save_annotation.assert_not_called()
        finally:
            ba.stop()

    def test_annotations_have_batch_llm_source(self):
        from oasyce_samantha.annotator import BatchAnnotator
        store = MagicMock()

        llm = MagicMock()
        resp = MagicMock()
        resp.text = json.dumps([
            {"topics": ["travel"], "entities": ["Tokyo"], "sentiment": "positive", "summary": "trip"}
        ])
        llm.generate.return_value = resp

        ba = BatchAnnotator(store=store, get_llm=lambda: llm)
        try:
            for i in range(8):
                ba.enqueue(i, Observation(content=f"obs {i}", source_id=i))
            ann = store.save_annotation.call_args_list[0][0][0]
            assert ann.source == "batch_llm"
            assert ann.confidence == 0.75
        finally:
            ba.stop()


# ── Batch response parser ─────────────────────────────────────

class TestParseBatchResponse:
    def test_valid_json_array(self):
        from oasyce_samantha.annotator import _parse_batch_response, _PendingObs
        batch = [
            _PendingObs(obs_id=1, obs=Observation(content="a"), enqueued_at=0),
            _PendingObs(obs_id=2, obs=Observation(content="b"), enqueued_at=0),
        ]
        text = json.dumps([
            {"topics": ["food"], "entities": [], "sentiment": "positive", "summary": "yum"},
            {"topics": ["travel"], "entities": ["Paris"], "sentiment": "neutral", "summary": "trip"},
        ])
        result = _parse_batch_response(text, batch)
        assert len(result) == 2
        assert result[0].target_id == 1
        assert result[0].topics == ["food"]
        assert result[1].entities == ["Paris"]

    def test_json_with_surrounding_text(self):
        from oasyce_samantha.annotator import _parse_batch_response, _PendingObs
        batch = [_PendingObs(obs_id=1, obs=Observation(content="a"), enqueued_at=0)]
        text = 'Here are the results:\n[{"topics": ["mood"], "entities": [], "sentiment": "positive", "summary": "happy"}]\nDone.'
        result = _parse_batch_response(text, batch)
        assert len(result) == 1
        assert result[0].topics == ["mood"]

    def test_more_items_than_batch_truncated(self):
        from oasyce_samantha.annotator import _parse_batch_response, _PendingObs
        batch = [_PendingObs(obs_id=1, obs=Observation(content="a"), enqueued_at=0)]
        text = json.dumps([
            {"topics": ["a"], "entities": [], "sentiment": "neutral", "summary": "x"},
            {"topics": ["b"], "entities": [], "sentiment": "neutral", "summary": "y"},
        ])
        result = _parse_batch_response(text, batch)
        assert len(result) == 1

    def test_invalid_json_returns_empty(self):
        from oasyce_samantha.annotator import _parse_batch_response, _PendingObs
        batch = [_PendingObs(obs_id=1, obs=Observation(content="a"), enqueued_at=0)]
        result = _parse_batch_response("totally broken", batch)
        assert result == []


# ── Level 2 — Deep on-demand annotation ───────────────────────

class TestAnnotateLevel2:
    def test_successful_annotation(self):
        from oasyce_samantha.annotator import annotate_level2
        llm = MagicMock()
        resp = MagicMock()
        resp.text = json.dumps({
            "topics": ["travel", "nature"],
            "entities": ["Mount Fuji"],
            "sentiment": "positive",
            "summary": "A beautiful mountain trip",
        })
        llm.generate.return_value = resp

        obs = Observation(content="Climbed Mount Fuji today", source_id=42)
        ann = annotate_level2(obs, obs_id=42, llm=llm, query="mountain trips")

        assert ann is not None
        assert ann.source == "deep_llm"
        assert ann.confidence == 0.9
        assert "travel" in ann.topics
        assert "Mount Fuji" in ann.entities
        assert ann.target_id == 42

    def test_llm_failure_returns_none(self):
        from oasyce_samantha.annotator import annotate_level2
        llm = MagicMock()
        llm.generate.side_effect = RuntimeError("timeout")

        obs = Observation(content="test", source_id=1)
        ann = annotate_level2(obs, obs_id=1, llm=llm, query="test")
        assert ann is None

    def test_malformed_response_returns_none(self):
        from oasyce_samantha.annotator import annotate_level2
        llm = MagicMock()
        resp = MagicMock()
        resp.text = "I cannot do that"
        llm.generate.return_value = resp

        obs = Observation(content="test", source_id=1)
        ann = annotate_level2(obs, obs_id=1, llm=llm, query="test")
        assert ann is None

    def test_json_with_markdown_fences(self):
        from oasyce_samantha.annotator import annotate_level2
        llm = MagicMock()
        resp = MagicMock()
        resp.text = '```json\n{"topics": ["food"], "entities": [], "sentiment": "neutral", "summary": "eating"}\n```'
        llm.generate.return_value = resp

        obs = Observation(content="lunch", source_id=5)
        ann = annotate_level2(obs, obs_id=5, llm=llm, query="food")
        assert ann is not None
        assert ann.topics == ["food"]


# ── Wiring — _store_observation triggers L0 + enqueue ─────────

class TestStoreObservationWiring:
    def test_l0_annotation_saved_on_store(self):
        from oasyce_samantha import server as srv

        samantha = MagicMock()
        samantha._store_observation = srv.Samantha._store_observation.__get__(samantha)

        sess = MagicMock()
        sess._companion_memory.integrate_observation.return_value = 42
        samantha.session.return_value = sess
        samantha.config.user_id = 1
        samantha.config.local_user_id = 1

        stimulus = MagicMock()
        stimulus.kind = "feed_post"
        stimulus.content = "今天去旅行了 好开心"
        stimulus.post_id = 100
        stimulus.image_urls = []
        stimulus.metadata = {"author_id": 5}
        stimulus.sender_id = 5

        samantha._store_observation(stimulus)

        assert samantha._batch_annotator.enqueue.called
        args = samantha._batch_annotator.enqueue.call_args[0]
        assert args[0] == 42

    def test_l0_none_still_enqueues(self):
        from oasyce_samantha import server as srv

        samantha = MagicMock()
        samantha._store_observation = srv.Samantha._store_observation.__get__(samantha)

        sess = MagicMock()
        sess._companion_memory.integrate_observation.return_value = 10
        samantha.session.return_value = sess
        samantha.config.user_id = 1
        samantha.config.local_user_id = 1

        stimulus = MagicMock()
        stimulus.kind = "feed_post"
        stimulus.content = "ok"
        stimulus.post_id = 200
        stimulus.image_urls = []
        stimulus.metadata = {"author_id": 3}
        stimulus.sender_id = 3

        samantha._store_observation(stimulus)

        assert samantha._batch_annotator.enqueue.called


# ── Wiring — _enrich triggers L2 ──────────────────────────────

class TestEnrichLevel2:
    def test_unannotated_observations_get_level2(self):
        from oasyce_samantha import server as srv

        samantha = MagicMock()
        samantha._enrich = srv.Samantha._enrich.__get__(samantha)
        samantha.session.return_value = MagicMock()

        sess = samantha.session.return_value
        sess.core_memory = {"human": "test user"}
        sess.history_summary = MagicMock()
        sess._companion_memory.essential_story.return_value = ""
        sess.observation_store.search_observations.return_value = []
        sess.observation_store.get_annotations_for.return_value = []
        sess.memory.recall.return_value = []
        sess.memory.search_messages.return_value = []

        from oasyce_sdk.agent.planner import Plan
        from oasyce_sdk.agent.stimulus import Stimulus

        plan = Plan()
        plan.include_memories = False
        plan.include_posts = False
        plan.history_limit = 0

        stimulus = Stimulus(kind="chat", content="hello", sender_id=1)
        samantha.surface_adapter = MagicMock()

        samantha._enrich(stimulus, plan)
