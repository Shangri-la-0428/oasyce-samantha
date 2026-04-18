"""Phase 7 contract tests — Streaming + Performance.

Validates:
  - Parallel perceive: sigil.perceive + ambient_priors run concurrently
  - Parallel dream: session summaries + independent tasks parallelize
  - Parallel maintenance: multi-user maintenance runs concurrently
  - Streaming inherits from SDK (process_stream available)
"""

from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from oasyce_sdk.agent.stimulus import Stimulus


# ── Parallel perceive ───────────────────────────────────────

class TestPerceiveParallel:
    def test_perceive_and_priors_both_called(self):
        from oasyce_samantha import server as srv

        stimulus = Stimulus(kind="chat", content="hello world", sender_id=1)

        samantha = MagicMock()
        samantha._perceive = srv.Samantha._perceive.__get__(samantha)

        perception = MagicMock()
        samantha.sigil.perceive.return_value = perception
        mock_priors = {"priors": [{"kind": "success-prior"}]}
        samantha.sigil.thronglets.ambient_priors.return_value = mock_priors
        samantha.sigil.space = "test"

        result = samantha._perceive(stimulus)

        assert result is perception
        samantha.sigil.perceive.assert_called_once()
        samantha.sigil.thronglets.ambient_priors.assert_called_once()
        assert stimulus.metadata["_ambient_priors"] == mock_priors

    def test_priors_failure_doesnt_break_perceive(self):
        from oasyce_samantha import server as srv

        stimulus = Stimulus(kind="chat", content="hello", sender_id=1)

        samantha = MagicMock()
        samantha._perceive = srv.Samantha._perceive.__get__(samantha)

        perception = MagicMock()
        samantha.sigil.perceive.return_value = perception
        samantha.sigil.thronglets.ambient_priors.side_effect = RuntimeError("down")
        samantha.sigil.space = "test"

        result = samantha._perceive(stimulus)

        assert result is perception
        assert "_ambient_priors" not in stimulus.metadata

    def test_concurrent_execution(self):
        """Verify both calls overlap (not serial)."""
        from oasyce_samantha import server as srv

        stimulus = Stimulus(kind="chat", content="test", sender_id=1)

        samantha = MagicMock()
        samantha._perceive = srv.Samantha._perceive.__get__(samantha)
        samantha.sigil.space = "test"

        call_log: list[tuple[str, float]] = []

        def slow_perceive(context):
            call_log.append(("perceive_start", time.monotonic()))
            time.sleep(0.05)
            call_log.append(("perceive_end", time.monotonic()))
            return MagicMock()

        def slow_priors(*a, **kw):
            call_log.append(("priors_start", time.monotonic()))
            time.sleep(0.05)
            call_log.append(("priors_end", time.monotonic()))
            return {"priors": []}

        samantha.sigil.perceive.side_effect = slow_perceive
        samantha.sigil.thronglets.ambient_priors.side_effect = slow_priors

        start = time.monotonic()
        samantha._perceive(stimulus)
        elapsed = time.monotonic() - start

        assert elapsed < 0.15, f"Expected parallel (~50ms), got {elapsed*1000:.0f}ms"

        starts = {name: t for name, t in call_log if name.endswith("_start")}
        assert abs(starts["perceive_start"] - starts["priors_start"]) < 0.03

    def test_goal_based_on_stimulus_kind(self):
        from oasyce_samantha import server as srv

        samantha = MagicMock()
        samantha._perceive = srv.Samantha._perceive.__get__(samantha)
        samantha.sigil.perceive.return_value = MagicMock()
        samantha.sigil.thronglets.ambient_priors.return_value = {}
        samantha.sigil.space = "test"

        samantha._perceive(Stimulus(kind="chat", content="hi", sender_id=1))
        _, kwargs = samantha.sigil.thronglets.ambient_priors.call_args
        assert kwargs["goal"] == "build"

        samantha.sigil.thronglets.ambient_priors.reset_mock()
        samantha._perceive(Stimulus(kind="feed_post", content="post", sender_id=1))
        _, kwargs = samantha.sigil.thronglets.ambient_priors.call_args
        assert kwargs["goal"] == "explore"


# ── Parallel dream ──────────────────────────────────────────

class TestDreamParallel:
    def test_dream_calls_all_phases(self):
        from oasyce_samantha import server as srv

        samantha = MagicMock()
        samantha.dream = srv.Samantha.dream.__get__(samantha)
        samantha._dream_summarize_session = MagicMock()
        samantha._dream_consolidate = MagicMock()
        samantha._dream_essential_story = MagicMock()
        samantha._dream_psyche_snapshot = MagicMock()
        samantha._dream_hebbian_boost = MagicMock()

        sess = MagicMock()
        sess.get_llm.return_value = MagicMock()
        sess.drain_active_sessions.return_value = [101, 102]

        samantha.dream(42, sess)

        assert samantha._dream_summarize_session.call_count == 2
        samantha._dream_consolidate.assert_called_once()
        samantha._dream_essential_story.assert_called_once()
        samantha._dream_psyche_snapshot.assert_called_once_with(42)
        samantha._dream_hebbian_boost.assert_called_once_with(sess)

    def test_dream_no_active_sessions(self):
        from oasyce_samantha import server as srv

        samantha = MagicMock()
        samantha.dream = srv.Samantha.dream.__get__(samantha)
        samantha._dream_summarize_session = MagicMock()
        samantha._dream_consolidate = MagicMock()
        samantha._dream_essential_story = MagicMock()
        samantha._dream_psyche_snapshot = MagicMock()
        samantha._dream_hebbian_boost = MagicMock()

        sess = MagicMock()
        sess.get_llm.return_value = MagicMock()
        sess.drain_active_sessions.return_value = []

        samantha.dream(42, sess)

        samantha._dream_summarize_session.assert_not_called()
        samantha._dream_consolidate.assert_called_once()

    def test_summarize_failure_doesnt_block_others(self):
        from oasyce_samantha import server as srv

        samantha = MagicMock()
        samantha.dream = srv.Samantha.dream.__get__(samantha)
        samantha._dream_summarize_session.side_effect = RuntimeError("boom")
        samantha._dream_consolidate = MagicMock()
        samantha._dream_essential_story = MagicMock()
        samantha._dream_psyche_snapshot = MagicMock()
        samantha._dream_hebbian_boost = MagicMock()

        sess = MagicMock()
        sess.get_llm.return_value = MagicMock()
        sess.drain_active_sessions.return_value = [1]

        samantha.dream(42, sess)

        samantha._dream_consolidate.assert_called_once()
        samantha._dream_psyche_snapshot.assert_called_once()

    def test_psyche_and_hebbian_parallel(self):
        """Phase 3 tasks run concurrently."""
        from oasyce_samantha import server as srv

        samantha = MagicMock()
        samantha.dream = srv.Samantha.dream.__get__(samantha)
        samantha._dream_consolidate = MagicMock()
        samantha._dream_essential_story = MagicMock()

        call_log: list[tuple[str, float]] = []

        def slow_psyche(user_id):
            call_log.append(("psyche_start", time.monotonic()))
            time.sleep(0.03)
            call_log.append(("psyche_end", time.monotonic()))

        def slow_hebbian(sess):
            call_log.append(("hebbian_start", time.monotonic()))
            time.sleep(0.03)
            call_log.append(("hebbian_end", time.monotonic()))

        samantha._dream_psyche_snapshot = slow_psyche
        samantha._dream_hebbian_boost = slow_hebbian

        sess = MagicMock()
        sess.get_llm.return_value = MagicMock()
        sess.drain_active_sessions.return_value = []

        start = time.monotonic()
        samantha.dream(42, sess)
        elapsed = time.monotonic() - start

        assert elapsed < 0.1, f"Expected parallel (~30ms), got {elapsed*1000:.0f}ms"


# ── Parallel maintenance ────────────────────────────────────

class TestMaintenanceParallel:
    def test_multiple_users_processed(self):
        from oasyce_samantha.streams import MaintenanceStream

        runtime = MagicMock()
        sess1 = MagicMock()
        sess2 = MagicMock()
        runtime._sessions = {1: sess1, 2: sess2}

        stream = MaintenanceStream(runtime, interval=3000)
        result = stream.poll()

        assert result == []
        assert sess1.memory.prune.called
        assert sess2.memory.prune.called
        assert runtime.dream.call_count == 2

    def test_empty_sessions(self):
        from oasyce_samantha.streams import MaintenanceStream

        runtime = MagicMock()
        runtime._sessions = {}

        stream = MaintenanceStream(runtime, interval=3000)
        result = stream.poll()
        assert result == []

    def test_one_user_failure_doesnt_block_others(self):
        from oasyce_samantha.streams import MaintenanceStream

        runtime = MagicMock()
        sess1 = MagicMock()
        sess1.memory.prune.side_effect = RuntimeError("db error")
        sess2 = MagicMock()
        runtime._sessions = {1: sess1, 2: sess2}

        stream = MaintenanceStream(runtime, interval=3000)
        stream.poll()

        assert sess2.memory.prune.called
        assert runtime.dream.call_count >= 1

    def test_concurrent_user_processing(self):
        from oasyce_samantha.streams import MaintenanceStream

        runtime = MagicMock()
        call_log: list[tuple[int, float]] = []

        def slow_dream(user_id, sess):
            call_log.append((user_id, time.monotonic()))
            time.sleep(0.03)

        runtime.dream.side_effect = slow_dream
        runtime._sessions = {
            i: MagicMock() for i in range(4)
        }

        stream = MaintenanceStream(runtime, interval=3000)
        start = time.monotonic()
        stream.poll()
        elapsed = time.monotonic() - start

        assert elapsed < 0.1, f"Expected parallel (~30ms), got {elapsed*1000:.0f}ms"
        assert len(call_log) == 4


# ── dream_summarize_session ─────────────────────────────────

class TestDreamSummarizeSession:
    def test_skips_when_no_update_needed(self):
        from oasyce_samantha import server as srv

        samantha = MagicMock()
        samantha._dream_summarize_session = srv.Samantha._dream_summarize_session.__get__(samantha)
        samantha._fetch_history.return_value = [MagicMock()] * 5

        sess = MagicMock()
        sess.user_id = 42
        sess.history_summary.needs_update.return_value = False

        samantha._dream_summarize_session(MagicMock(), sess, 101)

        samantha._dream_summarize.assert_not_called()

    def test_calls_summarize_when_needed(self):
        from oasyce_samantha import server as srv

        samantha = MagicMock()
        samantha._dream_summarize_session = srv.Samantha._dream_summarize_session.__get__(samantha)
        history = [MagicMock()] * 15
        samantha._fetch_history.return_value = history

        sess = MagicMock()
        sess.user_id = 42
        sess.history_summary.needs_update.return_value = True
        llm = MagicMock()

        samantha._dream_summarize_session(llm, sess, 101)

        samantha._dream_summarize.assert_called_once_with(llm, sess, 101, history)


# ── process_stream inheritance ──────────────────────────────

class TestSamanthaStreaming:
    def test_has_process_stream(self):
        from oasyce_samantha import server as srv
        assert hasattr(srv.Samantha, "process_stream")
