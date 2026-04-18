"""Phase 6 contract tests — Collective Knowledge.

Validates:
  - share_annotation posts signals to Thronglets
  - collect_annotations parses ambient_priors for cross-agent annotations
  - is_already_shared dedup check via signal_feed
  - boost_corroborated Hebbian reinforcement
  - _enrich injects collective annotations
  - _perceive stashes ambient_priors for _enrich
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from oasyce_sdk.agent.cognitive import Annotation, Observation
from oasyce_sdk.agent.stimulus import Stimulus

from oasyce_samantha.collective import (
    boost_corroborated,
    collect_annotations,
    is_already_shared,
    share_annotation,
)


# ── share_annotation ─────────────────────────────────────────

class TestShareAnnotation:
    def test_posts_signal(self):
        sigil = MagicMock()
        sigil.thronglets.signal_post.return_value = {"ok": True}
        sigil.space = "test-space"

        obs = Observation(source_type="post", source_id=42, content="Snow photo")
        ann = Annotation(
            topics=["travel"], entities=["Jade Dragon"], summary="Snow mountain post",
        )

        result = share_annotation(sigil, obs, ann)
        assert result is True

        sigil.thronglets.signal_post.assert_called_once()
        args = sigil.thronglets.signal_post.call_args
        assert args[1]["kind"] == "info"
        assert args[1]["space"] == "test-space"
        ctx = json.loads(args[1]["context"])
        assert ctx["type"] == "observation_annotation"
        assert ctx["source_id"] == 42
        assert ctx["topics"] == ["travel"]

    def test_skips_empty_summary(self):
        sigil = MagicMock()
        obs = Observation(source_type="post", content="test")
        ann = Annotation(summary="")

        result = share_annotation(sigil, obs, ann)
        assert result is False
        sigil.thronglets.signal_post.assert_not_called()

    def test_handles_network_failure(self):
        sigil = MagicMock()
        sigil.thronglets.signal_post.side_effect = RuntimeError("network")
        sigil.space = "test"

        obs = Observation(source_type="post", content="test")
        ann = Annotation(summary="test summary")

        result = share_annotation(sigil, obs, ann)
        assert result is False


# ── collect_annotations ──────────────────────────────────────

class TestCollectAnnotations:
    def test_parses_annotation_signals(self):
        priors = {
            "priors": [
                {
                    "kind": "success-prior",
                    "confidence": 0.8,
                    "summary": "Snow mountain scenery recommendation",
                    "context": json.dumps({
                        "type": "observation_annotation",
                        "source_id": 42,
                        "topics": ["travel"],
                        "entities": ["Jade Dragon"],
                    }),
                },
            ],
        }
        results = collect_annotations(priors)
        assert len(results) == 1
        assert results[0]["topics"] == ["travel"]
        assert results[0]["entities"] == ["Jade Dragon"]
        assert results[0]["source_id"] == 42
        assert results[0]["confidence"] == 0.8

    def test_ignores_non_annotation_signals(self):
        priors = {
            "priors": [
                {
                    "kind": "failure-residue",
                    "confidence": 0.9,
                    "summary": "Something failed",
                    "context": json.dumps({"type": "error_trace"}),
                },
            ],
        }
        assert collect_annotations(priors) == []

    def test_handles_malformed_context(self):
        priors = {
            "priors": [
                {"kind": "success-prior", "context": "not json"},
                {"kind": "success-prior", "context": json.dumps({"type": "other"})},
                {"kind": "success-prior"},
            ],
        }
        assert collect_annotations(priors) == []

    def test_handles_none_priors(self):
        assert collect_annotations(None) == []
        assert collect_annotations({}) == []

    def test_multiple_annotations(self):
        priors = {
            "priors": [
                {
                    "kind": "success-prior",
                    "summary": "First",
                    "context": json.dumps({
                        "type": "observation_annotation",
                        "source_id": 1,
                        "topics": ["a"],
                        "entities": [],
                    }),
                },
                {
                    "kind": "success-prior",
                    "summary": "Second",
                    "context": json.dumps({
                        "type": "observation_annotation",
                        "source_id": 2,
                        "topics": ["b"],
                        "entities": [],
                    }),
                },
            ],
        }
        results = collect_annotations(priors)
        assert len(results) == 2


# ── is_already_shared ────────────────────────────────────────

class TestIsAlreadyShared:
    def test_finds_existing_signal(self):
        sig = MagicMock()
        sig.context = json.dumps({
            "type": "observation_annotation",
            "source_id": 42,
        })

        sigil = MagicMock()
        sigil.thronglets.signal_feed.return_value = [sig]

        assert is_already_shared(sigil, 42) is True

    def test_returns_false_when_not_found(self):
        sig = MagicMock()
        sig.context = json.dumps({
            "type": "observation_annotation",
            "source_id": 99,
        })

        sigil = MagicMock()
        sigil.thronglets.signal_feed.return_value = [sig]

        assert is_already_shared(sigil, 42) is False

    def test_returns_false_on_failure(self):
        sigil = MagicMock()
        sigil.thronglets.signal_feed.side_effect = RuntimeError("down")

        assert is_already_shared(sigil, 42) is False

    def test_skips_zero_source_id(self):
        sigil = MagicMock()
        assert is_already_shared(sigil, 0) is False
        sigil.thronglets.signal_feed.assert_not_called()


# ── boost_corroborated ───────────────────────────────────────

class TestBoostCorroborated:
    def test_boosts_matching_observation(self, tmp_path):
        from oasyce_sdk.agent.store import ObservationStore

        store = ObservationStore(tmp_path / "test.db")
        store.save_observation(Observation(
            source_type="post", source_id=42, content="Snow trip",
            emotional_weight=0.5,
        ))

        priors = {
            "priors": [{
                "kind": "success-prior",
                "summary": "Snow annotation",
                "context": json.dumps({
                    "type": "observation_annotation",
                    "source_id": 42,
                    "topics": ["travel"],
                    "entities": [],
                }),
            }],
        }

        boosted = boost_corroborated(store, priors, {42})
        assert boosted == 1

        obs = store.get_observation(1)
        assert obs.emotional_weight == pytest.approx(0.6)

    def test_caps_at_1_0(self, tmp_path):
        from oasyce_sdk.agent.store import ObservationStore

        store = ObservationStore(tmp_path / "test.db")
        store.save_observation(Observation(
            source_type="post", source_id=10, content="test",
            emotional_weight=0.95,
        ))

        priors = {
            "priors": [{
                "kind": "success-prior",
                "summary": "test",
                "context": json.dumps({
                    "type": "observation_annotation",
                    "source_id": 10,
                    "topics": [],
                    "entities": [],
                }),
            }],
        }

        boost_corroborated(store, priors, {10})
        obs = store.get_observation(1)
        assert obs.emotional_weight <= 1.0

    def test_no_boost_for_non_local(self, tmp_path):
        from oasyce_sdk.agent.store import ObservationStore

        store = ObservationStore(tmp_path / "test.db")
        priors = {
            "priors": [{
                "kind": "success-prior",
                "summary": "test",
                "context": json.dumps({
                    "type": "observation_annotation",
                    "source_id": 99,
                    "topics": [],
                    "entities": [],
                }),
            }],
        }

        boosted = boost_corroborated(store, priors, {42})
        assert boosted == 0

    def test_empty_priors(self, tmp_path):
        from oasyce_sdk.agent.store import ObservationStore

        store = ObservationStore(tmp_path / "test.db")
        assert boost_corroborated(store, None, {42}) == 0
        assert boost_corroborated(store, {}, {42}) == 0


# ── _enrich collective injection ─────────────────────────────

class TestEnrichCollective:
    def test_collective_annotations_added_to_observations(self):
        from oasyce_samantha import server as srv

        stimulus = Stimulus(kind="chat", content="snow", sender_id=1, metadata={
            "_ambient_priors": {
                "priors": [{
                    "kind": "success-prior",
                    "summary": "Jade Dragon Snow Mountain recommended",
                    "context": json.dumps({
                        "type": "observation_annotation",
                        "source_id": 42,
                        "topics": ["travel"],
                        "entities": ["Jade Dragon"],
                    }),
                }],
            },
        })

        samantha = MagicMock()
        samantha._enrich = srv.Samantha._enrich.__get__(samantha)
        samantha.session.return_value = MagicMock()
        samantha.session.return_value.core_memory = MagicMock()
        samantha.session.return_value.history_summary.get.return_value = ""
        samantha.session.return_value._companion_memory.essential_story.return_value = ""
        samantha.session.return_value.memory.recall.return_value = []
        samantha.session.return_value.memory.search_messages.return_value = []
        samantha.session.return_value.observation_store.search_observations.return_value = []

        plan = MagicMock()
        plan.include_memories = False
        plan.include_posts = False
        plan.history_limit = 0

        ctx = samantha._enrich(stimulus, plan)
        collective = [o for o in ctx.observations if o.get("source_type") == "collective"]
        assert len(collective) == 1
        assert "Jade Dragon" in collective[0]["content"]

    def test_no_priors_no_injection(self):
        from oasyce_samantha import server as srv

        stimulus = Stimulus(kind="chat", content="hi", sender_id=1, metadata={})

        samantha = MagicMock()
        samantha._enrich = srv.Samantha._enrich.__get__(samantha)
        samantha.session.return_value = MagicMock()
        samantha.session.return_value.core_memory = MagicMock()
        samantha.session.return_value.history_summary.get.return_value = ""
        samantha.session.return_value._companion_memory.essential_story.return_value = ""

        plan = MagicMock()
        plan.include_memories = False
        plan.include_posts = False
        plan.history_limit = 0

        ctx = samantha._enrich(stimulus, plan)
        assert len(ctx.observations) == 0


# ── _perceive stashes priors ─────────────────────────────────

class TestPerceiveStash:
    def test_ambient_priors_stashed_in_metadata(self):
        from oasyce_samantha import server as srv

        stimulus = Stimulus(kind="chat", content="hello", sender_id=1)

        samantha = MagicMock()
        samantha._perceive = srv.Samantha._perceive.__get__(samantha)

        mock_priors = {"priors": [{"kind": "success-prior"}]}
        perception = MagicMock()
        samantha.sigil.perceive.return_value = perception
        samantha.sigil.thronglets.ambient_priors.return_value = mock_priors
        samantha.sigil.space = "test"

        samantha._perceive(stimulus)

        assert stimulus.metadata.get("_ambient_priors") == mock_priors

    def test_priors_failure_doesnt_crash(self):
        from oasyce_samantha import server as srv

        stimulus = Stimulus(kind="chat", content="hello", sender_id=1)

        samantha = MagicMock()
        samantha._perceive = srv.Samantha._perceive.__get__(samantha)

        perception = MagicMock()
        samantha.sigil.perceive.return_value = perception
        samantha.sigil.thronglets.ambient_priors.side_effect = RuntimeError("down")

        result = samantha._perceive(stimulus)
        assert result is perception
        assert "_ambient_priors" not in stimulus.metadata
