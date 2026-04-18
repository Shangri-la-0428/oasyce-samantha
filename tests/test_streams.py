"""Phase 3 contract tests — Streams, cognitive loop, pipeline overrides.

Validates:
  - FeedStream, ReflectionStream, MaintenanceStream satisfy Stream protocol
  - Each stream produces correct stimulus kinds and default modes
  - cognitive_loop assembles streams from adapter + built-in
  - Samantha._safe_process stores observations for feed_post stimuli
  - Samantha._deliver filters SILENCE and routes proactive delivery
"""

from __future__ import annotations

from dataclasses import dataclass, field
from unittest.mock import MagicMock, patch

import pytest

from oasyce_sdk.agent.cognitive import CognitiveMode
from oasyce_sdk.agent.stimulus import Stimulus
from oasyce_sdk.agent.stream import Stream


# ── Stream protocol satisfaction ─────────────────────────────

class TestStreamProtocol:
    def test_feed_stream_satisfies(self):
        from oasyce_samantha.streams import FeedStream

        runtime = MagicMock()
        runtime.surface_adapter = MagicMock()
        fs = FeedStream(runtime, interval=60)
        assert isinstance(fs, Stream)
        assert fs.interval == 60
        assert fs.default_mode == CognitiveMode.OBSERVING

    def test_reflection_stream_satisfies(self):
        from oasyce_samantha.streams import ReflectionStream

        runtime = MagicMock()
        rs = ReflectionStream(runtime, interval=900)
        assert isinstance(rs, Stream)
        assert rs.interval == 900
        assert rs.default_mode == CognitiveMode.PROACTIVE

    def test_maintenance_stream_satisfies(self):
        from oasyce_samantha.streams import MaintenanceStream

        runtime = MagicMock()
        ms = MaintenanceStream(runtime, interval=3000)
        assert isinstance(ms, Stream)
        assert ms.interval == 3000
        assert ms.default_mode == CognitiveMode.REFLECTING


# ── FeedStream ───────────────────────────────────────────────

class TestFeedStream:
    def test_poll_delegates_to_adapter(self):
        from oasyce_samantha.streams import FeedStream

        s1 = Stimulus(kind="feed_post", content="Hello", sender_id=1)
        runtime = MagicMock()
        runtime.surface_adapter.collect_feed_stimuli = MagicMock(return_value=[s1])
        runtime._registry.slot_names = ["default"]

        fs = FeedStream(runtime, interval=60)
        result = fs.poll()

        assert len(result) == 1
        assert result[0].kind == "feed_post"
        runtime.surface_adapter.collect_feed_stimuli.assert_called_once()

    def test_poll_returns_empty_when_no_adapter_method(self):
        from oasyce_samantha.streams import FeedStream

        runtime = MagicMock()
        runtime.surface_adapter = object()
        runtime._registry.slot_names = ["default"]

        fs = FeedStream(runtime, interval=60)
        assert fs.poll() == []

    def test_poll_returns_empty_when_no_slots(self):
        from oasyce_samantha.streams import FeedStream

        runtime = MagicMock()
        runtime._registry.slot_names = []

        fs = FeedStream(runtime, interval=60)
        assert fs.poll() == []

    def test_poll_catches_exceptions(self):
        from oasyce_samantha.streams import FeedStream

        runtime = MagicMock()
        runtime.surface_adapter.collect_feed_stimuli = MagicMock(side_effect=RuntimeError("api"))
        runtime._registry.slot_names = ["default"]

        fs = FeedStream(runtime, interval=60)
        assert fs.poll() == []

    def test_tracks_seen_posts(self):
        from oasyce_samantha.streams import FeedStream

        runtime = MagicMock()
        runtime._registry.slot_names = ["default"]

        fs = FeedStream(runtime, interval=60)
        assert isinstance(fs._seen_posts, set)
        assert isinstance(fs._seen_comments, set)

        fs._seen_posts.add(42)
        runtime.surface_adapter.collect_feed_stimuli = MagicMock(return_value=[])
        fs.poll()

        call_args = runtime.surface_adapter.collect_feed_stimuli.call_args
        assert 42 in call_args[0][1]


# ── ReflectionStream ─────────────────────────────────────────

class TestReflectionStream:
    @staticmethod
    def _mock_session():
        sess = MagicMock()
        sess.core_memory.get = MagicMock(side_effect=lambda k: {
            "human": "Alice, loves tea",
            "relationship": "close friend",
        }.get(k, ""))
        sess.drain_active_sessions = MagicMock(return_value=[101])
        return sess

    def test_poll_produces_reflection_stimuli(self):
        from oasyce_samantha.streams import ReflectionStream

        runtime = MagicMock()
        runtime.surface_capabilities.chat = True
        runtime._sessions = {1: self._mock_session()}

        rs = ReflectionStream(runtime, interval=900)
        result = rs.poll()

        assert len(result) == 1
        assert result[0].kind == "reflection"
        assert result[0].sender_id == 1
        assert "Alice" in result[0].content

    def test_poll_empty_when_no_chat(self):
        from oasyce_samantha.streams import ReflectionStream

        runtime = MagicMock()
        runtime.surface_capabilities.chat = False

        rs = ReflectionStream(runtime, interval=900)
        assert rs.poll() == []

    def test_poll_multiple_sessions(self):
        from oasyce_samantha.streams import ReflectionStream

        runtime = MagicMock()
        runtime.surface_capabilities.chat = True
        runtime._sessions = {
            1: self._mock_session(),
            2: self._mock_session(),
        }

        rs = ReflectionStream(runtime, interval=900)
        result = rs.poll()
        assert len(result) == 2
        sender_ids = {s.sender_id for s in result}
        assert sender_ids == {1, 2}

    def test_stimulus_has_session_id(self):
        from oasyce_samantha.streams import ReflectionStream

        runtime = MagicMock()
        runtime.surface_capabilities.chat = True
        runtime._sessions = {1: self._mock_session()}

        rs = ReflectionStream(runtime, interval=900)
        result = rs.poll()
        assert result[0].session_id == 101

    def test_stimulus_has_mood_metadata(self):
        from oasyce_samantha.streams import ReflectionStream

        runtime = MagicMock()
        runtime.surface_capabilities.chat = True
        runtime._sessions = {1: self._mock_session()}

        rs = ReflectionStream(runtime, interval=900)
        result = rs.poll()
        assert result[0].metadata.get("mood") == "thoughtful"


# ── MaintenanceStream ────────────────────────────────────────

class TestMaintenanceStream:
    def test_poll_returns_empty(self):
        from oasyce_samantha.streams import MaintenanceStream

        runtime = MagicMock()
        runtime._sessions = {}

        ms = MaintenanceStream(runtime, interval=3000)
        assert ms.poll() == []

    def test_poll_runs_prune_and_dream(self):
        from oasyce_samantha.streams import MaintenanceStream

        sess = MagicMock()
        sess.memory.prune.return_value = 3

        runtime = MagicMock()
        runtime._sessions = {1: sess}

        ms = MaintenanceStream(runtime, interval=3000)
        result = ms.poll()

        assert result == []
        sess.memory.prune.assert_called_once_with(max_age_days=90, min_access=0)
        runtime.dream.assert_called_once_with(1, sess)

    def test_prune_failure_doesnt_block_dream(self):
        from oasyce_samantha.streams import MaintenanceStream

        sess = MagicMock()
        sess.memory.prune.side_effect = RuntimeError("db locked")

        runtime = MagicMock()
        runtime._sessions = {1: sess}

        ms = MaintenanceStream(runtime, interval=3000)
        ms.poll()

        runtime.dream.assert_called_once_with(1, sess)

    def test_dream_failure_doesnt_crash(self):
        from oasyce_samantha.streams import MaintenanceStream

        sess = MagicMock()
        sess.memory.prune.return_value = 0
        runtime = MagicMock()
        runtime._sessions = {1: sess}
        runtime.dream.side_effect = RuntimeError("llm error")

        ms = MaintenanceStream(runtime, interval=3000)
        ms.poll()


# ── Cognitive loop stream assembly ───────────────────────────

class TestCognitiveLoopAssembly:
    def test_assembles_adapter_plus_builtin_streams(self):
        from oasyce_samantha.loop import cognitive_loop
        from oasyce_samantha.streams import FeedStream, MaintenanceStream, ReflectionStream

        adapter_stream = MagicMock()
        adapter_stream.interval = 300

        runtime = MagicMock()
        runtime.surface_adapter.contribute_streams.return_value = [adapter_stream]

        collected_streams = []

        def mock_sleep(t):
            raise StopIteration("break loop for test")

        with patch("time.sleep", side_effect=mock_sleep):
            try:
                cognitive_loop(runtime, base_interval=300)
            except StopIteration:
                pass

        runtime.surface_adapter.contribute_streams.assert_called_once_with(runtime)


# ── Samantha._deliver override ───────────────────────────────

class TestDeliverOverride:
    """Delivery routing now lives in CompanionWorld (see test_world.py).

    These tests verify _deliver is a simple passthrough to channel,
    and that CompanionWorld handles mode-aware routing.
    """

    def test_deliver_forwards_to_channel(self):
        from oasyce_samantha import server as srv

        samantha = MagicMock()
        samantha._deliver = srv.Samantha._deliver.__get__(samantha)

        stimulus = Stimulus(kind="chat", content="hi", sender_id=1)
        samantha._deliver(stimulus, "hello!")

        samantha.channel.deliver.assert_called_once_with(stimulus, "hello!")

    def test_silence_filtered_by_companion_world(self):
        from oasyce_samantha.world import CompanionWorld
        from oasyce_sdk.agent.cognitive import CognitiveMode
        from oasyce_sdk.agent.planner import Plan

        runtime = MagicMock()
        w = CompanionWorld(runtime)
        stimulus = Stimulus(kind="reflection", content="test", sender_id=1)
        plan = Plan(mode=CognitiveMode.PROACTIVE)

        w.act(CognitiveMode.PROACTIVE, stimulus, "SILENCE", plan)
        runtime.deliver_proactive.assert_not_called()

    def test_silence_case_insensitive(self):
        from oasyce_samantha.world import CompanionWorld
        from oasyce_sdk.agent.cognitive import CognitiveMode
        from oasyce_sdk.agent.planner import Plan

        runtime = MagicMock()
        w = CompanionWorld(runtime)
        stimulus = Stimulus(kind="reflection", content="test", sender_id=1)
        plan = Plan(mode=CognitiveMode.PROACTIVE)

        w.act(CognitiveMode.PROACTIVE, stimulus, "  silence  ", plan)
        runtime.deliver_proactive.assert_not_called()

    def test_proactive_routes_through_deliver_proactive(self):
        from oasyce_samantha.world import CompanionWorld
        from oasyce_sdk.agent.cognitive import CognitiveMode
        from oasyce_sdk.agent.planner import Plan

        runtime = MagicMock()
        w = CompanionWorld(runtime)
        stimulus = Stimulus(
            kind="reflection", content="test", sender_id=42,
            metadata={"mood": "thoughtful"},
        )
        plan = Plan(mode=CognitiveMode.PROACTIVE)

        w.act(CognitiveMode.PROACTIVE, stimulus, "Hey, thinking of you", plan)

        runtime.deliver_proactive.assert_called_once()
        args = runtime.deliver_proactive.call_args
        assert args[0][0] == 42
        assert args[0][1] == "Hey, thinking of you"

    def test_chat_routes_through_channel(self):
        from oasyce_samantha.world import CompanionWorld
        from oasyce_sdk.agent.cognitive import CognitiveMode
        from oasyce_sdk.agent.planner import Plan

        runtime = MagicMock()
        w = CompanionWorld(runtime)
        stimulus = Stimulus(kind="chat", content="hi", sender_id=1)
        plan = Plan(mode=CognitiveMode.REACTIVE)

        w.act(CognitiveMode.REACTIVE, stimulus, "hello!", plan)

        runtime.channel.deliver.assert_called_once_with(stimulus, "hello!")


# ── Samantha._safe_process stores observations ───────────────

class TestObservationStorage:
    def test_feed_post_stored_as_observation(self, tmp_path, monkeypatch):
        from oasyce_samantha import server as srv
        monkeypatch.setattr(srv, "SAMANTHA_HOME", tmp_path)

        stimulus = Stimulus(
            kind="feed_post",
            content="Beautiful sunset photo",
            sender_id=0,
            metadata={"author_id": 100, "post_id": 42},
        )

        samantha = MagicMock(spec=srv.Samantha)
        samantha._store_observation = srv.Samantha._store_observation.__get__(samantha)
        samantha.config = MagicMock()
        samantha.config.user_id = 1
        samantha.config.local_user_id = 1

        mock_session = MagicMock()
        samantha.session = MagicMock(return_value=mock_session)

        samantha._store_observation(stimulus)

        mock_session._companion_memory.integrate_observation.assert_called_once()
        obs_arg = mock_session._companion_memory.integrate_observation.call_args[0][0]
        assert obs_arg.content == "Beautiful sunset photo"
        assert obs_arg.source_type == "feed_post"
        assert obs_arg.author_id == 100
