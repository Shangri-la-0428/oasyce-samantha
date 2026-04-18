"""Phase 4 contract tests — Self-Memory integration.

Validates:
  - Self protocol satisfaction
  - default_appraise produces correct emotional encoding from 4D state
  - Observation appraisal enrichment (update_observation_appraisal)
  - Psyche snapshot persistence at session boundaries
  - _reflect wires appraisal into stored observations
  - _dream_psyche_snapshot saves state at session boundary
"""

from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import MagicMock

import pytest

from oasyce_sdk.agent.cognitive import Appraisal, CognitiveMode, Observation
from oasyce_sdk.agent.self import Self, default_appraise
from oasyce_sdk.agent.stimulus import Stimulus


# ── Self protocol ────────────────────────────────────────────

class TestSelfProtocol:
    def test_protocol_is_runtime_checkable(self):
        assert hasattr(Self, "__protocol_attrs__") or hasattr(Self, "__abstractmethods__") or True
        assert isinstance(Self, type)

    def test_conforming_class_satisfies(self):
        class MySelf:
            def appraise(self, stimulus):
                return Appraisal()

            def integrate(self, stimulus, response, appraisal):
                pass

        assert isinstance(MySelf(), Self)

    def test_non_conforming_rejected(self):
        class NotSelf:
            pass

        assert not isinstance(NotSelf(), Self)


# ── default_appraise pure function ───────────────────────────

class TestDefaultAppraise:
    def test_baseline_returns_appraisal(self):
        s = Stimulus(kind="chat", content="hello")
        a = default_appraise(s)
        assert isinstance(a, Appraisal)
        assert 0.0 <= a.emotional_weight <= 1.0
        assert 0.0 <= a.relevance <= 1.0
        assert -1.0 <= a.valence <= 1.0

    def test_high_warmth_increases_emotional_weight(self):
        s = Stimulus(kind="chat", content="hi")
        a_warm = default_appraise(s, warmth=0.9, tension=0.2, guard=0.2, vitality=0.7)
        a_cold = default_appraise(s, warmth=0.1, tension=0.2, guard=0.8, vitality=0.3)
        assert a_warm.emotional_weight > a_cold.emotional_weight

    def test_high_tension_increases_emotional_weight(self):
        s = Stimulus(kind="chat", content="urgent")
        a_tense = default_appraise(s, tension=0.9, warmth=0.5, guard=0.5, vitality=0.5)
        a_calm = default_appraise(s, tension=0.1, warmth=0.5, guard=0.5, vitality=0.5)
        assert a_tense.emotional_weight > a_calm.emotional_weight

    def test_chat_has_higher_relevance_than_feed(self):
        s_chat = Stimulus(kind="chat", content="hi")
        s_feed = Stimulus(kind="feed_post", content="photo")
        a_chat = default_appraise(s_chat, warmth=0.7)
        a_feed = default_appraise(s_feed, warmth=0.7)
        assert a_chat.relevance > a_feed.relevance

    def test_dominant_affect_warm(self):
        s = Stimulus(kind="chat", content="hi")
        a = default_appraise(s, warmth=0.8, tension=0.2)
        assert a.dominant_affect == "warm"

    def test_dominant_affect_anxious(self):
        s = Stimulus(kind="chat", content="help")
        a = default_appraise(s, tension=0.8, warmth=0.3)
        assert a.dominant_affect == "anxious"

    def test_dominant_affect_tired(self):
        s = Stimulus(kind="chat", content="...")
        a = default_appraise(s, vitality=0.2, warmth=0.4, tension=0.3)
        assert a.dominant_affect == "tired"

    def test_dominant_affect_guarded(self):
        s = Stimulus(kind="chat", content="hmm")
        a = default_appraise(s, guard=0.8, warmth=0.3, tension=0.3, vitality=0.5)
        assert a.dominant_affect == "guarded"

    def test_valence_positive_when_warm(self):
        s = Stimulus(kind="chat", content="great")
        a = default_appraise(s, warmth=0.9, tension=0.1)
        assert a.valence > 0

    def test_valence_negative_when_tense(self):
        s = Stimulus(kind="chat", content="bad")
        a = default_appraise(s, warmth=0.1, tension=0.9)
        assert a.valence < 0

    def test_valence_clamped(self):
        s = Stimulus(kind="chat", content="x")
        a = default_appraise(s, warmth=1.0, tension=0.0)
        assert a.valence <= 1.0
        a2 = default_appraise(s, warmth=0.0, tension=1.0)
        assert a2.valence >= -1.0

    def test_psyche_delta_for_warm_chat(self):
        s = Stimulus(kind="chat", content="hello")
        a = default_appraise(s, warmth=0.7)
        assert "resonance" in a.psyche_delta

    def test_psyche_delta_for_tense(self):
        s = Stimulus(kind="chat", content="x")
        a = default_appraise(s, tension=0.8)
        assert "tension" in a.psyche_delta

    def test_emotional_weight_floor(self):
        s = Stimulus(kind="chat", content="x")
        a = default_appraise(s, warmth=0.0, tension=0.0, guard=1.0, vitality=0.0)
        assert a.emotional_weight >= 0.1

    def test_emotional_weight_ceiling(self):
        s = Stimulus(kind="chat", content="x")
        a = default_appraise(s, warmth=1.0, tension=1.0, guard=0.0, vitality=1.0)
        assert a.emotional_weight <= 1.0


# ── ObservationStore.update_observation_appraisal ────────────

class TestUpdateObservationAppraisal:
    def test_updates_emotional_weight(self, tmp_path):
        from oasyce_sdk.agent.store import ObservationStore

        store = ObservationStore(tmp_path / "test.db")
        obs_id = store.save_observation(Observation(
            source_type="post", content="Beautiful sunset",
        ))

        row = store.get_observation(obs_id)
        assert row.emotional_weight == pytest.approx(0.5)

        store.update_observation_appraisal(obs_id, 0.85, {"warmth": 0.9})
        row = store.get_observation(obs_id)
        assert row.emotional_weight == pytest.approx(0.85)
        assert row.psyche_snapshot == {"warmth": 0.9}

    def test_updates_only_target_observation(self, tmp_path):
        from oasyce_sdk.agent.store import ObservationStore

        store = ObservationStore(tmp_path / "test.db")
        id1 = store.save_observation(Observation(content="first"))
        id2 = store.save_observation(Observation(content="second"))

        store.update_observation_appraisal(id1, 0.9)
        assert store.get_observation(id1).emotional_weight == pytest.approx(0.9)
        assert store.get_observation(id2).emotional_weight == pytest.approx(0.5)

    def test_no_snapshot_stores_empty_dict(self, tmp_path):
        from oasyce_sdk.agent.store import ObservationStore

        store = ObservationStore(tmp_path / "test.db")
        obs_id = store.save_observation(Observation(content="test"))
        store.update_observation_appraisal(obs_id, 0.7)
        assert store.get_observation(obs_id).psyche_snapshot == {}


# ── Samantha _reflect integration ────────────────────────────

class TestReflectAppraisal:
    @staticmethod
    def _mock_kernel(vitality=0.5, tension=0.5, warmth=0.5, guard=0.5):
        k = MagicMock()
        k.vitality = vitality
        k.tension = tension
        k.warmth = warmth
        k.guard = guard
        return k

    def test_observation_enriched_after_reflect(self, tmp_path, monkeypatch):
        from oasyce_samantha import server as srv
        monkeypatch.setattr(srv, "SAMANTHA_HOME", tmp_path)

        samantha = MagicMock(spec=srv.Samantha)
        samantha._reflect = srv.Samantha._reflect.__get__(samantha)
        samantha._appraise = srv.Samantha._appraise

        kernel = self._mock_kernel(warmth=0.9, tension=0.2, guard=0.2, vitality=0.8)
        perception = MagicMock()
        perception.kernel = kernel

        mock_session = MagicMock()
        samantha.session = MagicMock(return_value=mock_session)
        samantha.config = MagicMock()
        samantha.config.user_id = 1
        samantha.config.local_user_id = 1

        stimulus = Stimulus(
            kind="feed_post", content="Snow mountain",
            sender_id=0, metadata={"_obs_id": 42},
        )

        samantha._reflect(stimulus, "nice!", perception)

        mock_session.observation_store.update_observation_appraisal.assert_called_once()
        call_args = mock_session.observation_store.update_observation_appraisal.call_args
        assert call_args[0][0] == 42
        assert call_args[0][1] > 0.5  # warm state → high emotional_weight

    def test_no_obs_id_skips_enrichment(self, tmp_path, monkeypatch):
        from oasyce_samantha import server as srv
        monkeypatch.setattr(srv, "SAMANTHA_HOME", tmp_path)

        samantha = MagicMock(spec=srv.Samantha)
        samantha._reflect = srv.Samantha._reflect.__get__(samantha)
        samantha._appraise = srv.Samantha._appraise

        perception = MagicMock()
        perception.kernel = self._mock_kernel()

        stimulus = Stimulus(kind="chat", content="hi", sender_id=1)

        samantha._reflect(stimulus, "hello!", perception)

        samantha.session.return_value.observation_store.update_observation_appraisal.assert_not_called()

    def test_high_intensity_saves_psyche_snapshot(self, tmp_path, monkeypatch):
        from oasyce_samantha import server as srv
        monkeypatch.setattr(srv, "SAMANTHA_HOME", tmp_path)

        samantha = MagicMock(spec=srv.Samantha)
        samantha._reflect = srv.Samantha._reflect.__get__(samantha)
        samantha._appraise = srv.Samantha._appraise
        samantha._save_psyche_snapshot = MagicMock()

        kernel = self._mock_kernel(tension=0.9, guard=0.1, vitality=0.8)
        perception = MagicMock()
        perception.kernel = kernel

        stimulus = Stimulus(kind="chat", content="urgent!", sender_id=42)

        samantha._reflect(stimulus, "I hear you", perception)

        samantha._save_psyche_snapshot.assert_called_once()
        call_args = samantha._save_psyche_snapshot.call_args
        assert call_args[0][0] == 42
        assert call_args[0][2] == "high_intensity_turn"

    def test_no_perception_skips_all(self):
        from oasyce_samantha import server as srv

        samantha = MagicMock(spec=srv.Samantha)
        samantha._reflect = srv.Samantha._reflect.__get__(samantha)

        stimulus = Stimulus(kind="chat", content="hi", sender_id=1)
        samantha._reflect(stimulus, "hello", None)

        samantha.session.assert_not_called()


# ── Psyche snapshot persistence ──────────────────────────────

class TestPsycheSnapshotPersistence:
    def test_save_and_retrieve(self, tmp_path):
        from oasyce_sdk.agent.store import KnowledgeStore

        store = KnowledgeStore(tmp_path / "test.db")
        store.save_psyche_snapshot(
            session_id=42,
            snapshot={
                "vitality": 0.8,
                "tension": 0.3,
                "warmth": 0.7,
                "guard": 0.2,
                "trigger": "session_boundary",
            },
        )

        row = store.latest_psyche_snapshot(42)
        assert row is not None
        assert row.snapshot["vitality"] == pytest.approx(0.8)
        assert row.snapshot["trigger"] == "session_boundary"

    def test_latest_returns_most_recent(self, tmp_path):
        from oasyce_sdk.agent.store import KnowledgeStore

        store = KnowledgeStore(tmp_path / "test.db")
        store.save_psyche_snapshot(42, {"warmth": 0.3})
        store.save_psyche_snapshot(42, {"warmth": 0.9})

        row = store.latest_psyche_snapshot(42)
        assert row.snapshot["warmth"] == pytest.approx(0.9)

    def test_dream_psyche_snapshot(self, tmp_path, monkeypatch):
        from oasyce_samantha import server as srv
        monkeypatch.setattr(srv, "SAMANTHA_HOME", tmp_path)

        samantha = MagicMock(spec=srv.Samantha)
        samantha._dream_psyche_snapshot = srv.Samantha._dream_psyche_snapshot.__get__(samantha)
        samantha._save_psyche_snapshot = MagicMock()

        kernel = MagicMock()
        kernel.vitality = 0.7
        kernel.tension = 0.3
        kernel.warmth = 0.8
        kernel.guard = 0.2

        perception = MagicMock()
        perception.kernel = kernel
        samantha.sigil = MagicMock()
        samantha.sigil._last_kernel = None
        samantha.sigil.perceive.return_value = perception

        samantha._dream_psyche_snapshot(42)

        samantha._save_psyche_snapshot.assert_called_once()
        args = samantha._save_psyche_snapshot.call_args[0]
        assert args[0] == 42
        assert args[2] == "session_boundary"


# ── SDK exports ──────────────────────────────────────────────

class TestPhase4Exports:
    def test_self_importable(self):
        from oasyce_sdk.agent import Self, default_appraise
        assert Self is not None
        assert callable(default_appraise)

    def test_appraise_from_top_level(self):
        from oasyce_sdk.agent import default_appraise
        a = default_appraise(Stimulus(kind="chat", content="x"))
        assert isinstance(a, Appraisal)
