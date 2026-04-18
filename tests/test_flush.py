"""Feature 10 contract tests — Pre-compaction Flush.

Validates:
  - Session counters: record_turn, needs_flush, is_idle, reset_counters
  - _log_turn triggers flush when threshold exceeded
  - _flush_session reuses dream sub-tasks
  - MaintenanceStream idle check
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import pytest

from oasyce_sdk.agent.stimulus import Stimulus


# ── Session counters ───────────────────────────────────────────

class TestSessionCounters:
    def _make_session(self):
        from oasyce_samantha.server import Session
        sess = MagicMock(spec=Session)
        sess._turn_count = 0
        sess._estimated_tokens = 0
        sess._last_turn_time = 0.0
        sess.record_turn = Session.record_turn.__get__(sess)
        sess.needs_flush = Session.needs_flush.__get__(sess)
        sess.is_idle = Session.is_idle.__get__(sess)
        sess.reset_counters = Session.reset_counters.__get__(sess)
        return sess

    def test_record_turn_increments_count(self):
        sess = self._make_session()
        sess.record_turn("hello", "hi there")
        assert sess._turn_count == 1

    def test_record_turn_estimates_tokens(self):
        sess = self._make_session()
        sess.record_turn("a" * 100, "b" * 200)
        assert sess._estimated_tokens == 150  # (100 + 200) / 2

    def test_record_turn_updates_last_time(self):
        sess = self._make_session()
        sess.record_turn("hi", "hello")
        assert sess._last_turn_time > 0

    def test_needs_flush_false_initially(self):
        sess = self._make_session()
        assert sess.needs_flush() is False

    def test_needs_flush_by_tokens(self):
        sess = self._make_session()
        sess._estimated_tokens = 80000
        assert sess.needs_flush() is True

    def test_needs_flush_by_turns(self):
        sess = self._make_session()
        sess._turn_count = 40
        assert sess.needs_flush() is True

    def test_needs_flush_custom_threshold(self):
        sess = self._make_session()
        sess._turn_count = 5
        assert sess.needs_flush(turn_threshold=5) is True
        assert sess.needs_flush(turn_threshold=10) is False

    def test_is_idle_false_initially(self):
        sess = self._make_session()
        assert sess.is_idle() is False

    def test_is_idle_false_when_recent(self):
        sess = self._make_session()
        sess.record_turn("hi", "hello")
        assert sess.is_idle(timeout=1800.0) is False

    def test_is_idle_true_after_timeout(self):
        sess = self._make_session()
        sess._last_turn_time = time.monotonic() - 2000
        assert sess.is_idle(timeout=1800.0) is True

    def test_is_idle_custom_timeout(self):
        sess = self._make_session()
        sess._last_turn_time = time.monotonic() - 5
        assert sess.is_idle(timeout=3.0) is True
        assert sess.is_idle(timeout=10.0) is False

    def test_reset_counters(self):
        sess = self._make_session()
        sess._turn_count = 50
        sess._estimated_tokens = 90000
        sess.reset_counters()
        assert sess._turn_count == 0
        assert sess._estimated_tokens == 0

    def test_accumulates_across_turns(self):
        sess = self._make_session()
        sess.record_turn("a" * 100, "b" * 100)
        sess.record_turn("c" * 200, "d" * 200)
        assert sess._turn_count == 2
        assert sess._estimated_tokens == 300  # 100 + 200


# ── _log_turn flush trigger ────────────────────────────────────

class TestLogTurnFlushTrigger:
    def test_flush_triggered_when_threshold_exceeded(self):
        from oasyce_samantha import server as srv

        samantha = MagicMock()
        samantha._log_turn = srv.Samantha._log_turn.__get__(samantha)

        sess = MagicMock()
        sess.needs_flush.return_value = True
        samantha.session.return_value = sess

        stimulus = Stimulus(kind="chat", content="hello", sender_id=1, session_id=10)
        samantha._log_turn(stimulus, "response")

        sess.record_turn.assert_called_once_with("hello", "response")
        samantha._executor.submit.assert_called_once()
        args = samantha._executor.submit.call_args[0]
        assert args[0] == samantha._flush_session
        assert args[1] == 1
        assert args[2] is sess

    def test_no_flush_when_under_threshold(self):
        from oasyce_samantha import server as srv

        samantha = MagicMock()
        samantha._log_turn = srv.Samantha._log_turn.__get__(samantha)

        sess = MagicMock()
        sess.needs_flush.return_value = False
        samantha.session.return_value = sess

        stimulus = Stimulus(kind="chat", content="hello", sender_id=1)
        samantha._log_turn(stimulus, "response")

        sess.record_turn.assert_called_once()
        samantha._executor.submit.assert_not_called()

    def test_non_chat_skipped(self):
        from oasyce_samantha import server as srv

        samantha = MagicMock()
        samantha._log_turn = srv.Samantha._log_turn.__get__(samantha)

        stimulus = Stimulus(kind="feed_post", content="post", sender_id=1)
        samantha._log_turn(stimulus, "comment")

        samantha.session.assert_not_called()

    def test_no_sender_skipped(self):
        from oasyce_samantha import server as srv

        samantha = MagicMock()
        samantha._log_turn = srv.Samantha._log_turn.__get__(samantha)

        stimulus = Stimulus(kind="chat", content="hello", sender_id=0)
        samantha._log_turn(stimulus, "response")

        samantha.session.assert_not_called()


# ── _flush_session ─────────────────────────────────────────────

class TestFlushSession:
    def test_summarize_and_consolidate(self):
        from oasyce_samantha import server as srv

        samantha = MagicMock()
        samantha._flush_session = srv.Samantha._flush_session.__get__(samantha)

        sess = MagicMock()
        sess.get_llm.return_value = MagicMock()
        sess.drain_active_sessions.return_value = [101, 102]

        samantha._flush_session(42, sess)

        assert samantha._dream_summarize_session.call_count == 2
        samantha._dream_consolidate.assert_called_once()
        sess.reset_counters.assert_called_once()

    def test_no_llm_returns_early(self):
        from oasyce_samantha import server as srv

        samantha = MagicMock()
        samantha._flush_session = srv.Samantha._flush_session.__get__(samantha)

        sess = MagicMock()
        sess.get_llm.return_value = None

        samantha._flush_session(42, sess)

        samantha._dream_summarize_session.assert_not_called()
        sess.reset_counters.assert_not_called()

    def test_no_active_sessions_still_consolidates(self):
        from oasyce_samantha import server as srv

        samantha = MagicMock()
        samantha._flush_session = srv.Samantha._flush_session.__get__(samantha)

        sess = MagicMock()
        sess.get_llm.return_value = MagicMock()
        sess.drain_active_sessions.return_value = []

        samantha._flush_session(42, sess)

        samantha._dream_summarize_session.assert_not_called()
        samantha._dream_consolidate.assert_called_once()
        sess.reset_counters.assert_called_once()

    def test_summarize_failure_doesnt_block_consolidate(self):
        from oasyce_samantha import server as srv

        samantha = MagicMock()
        samantha._flush_session = srv.Samantha._flush_session.__get__(samantha)
        samantha._dream_summarize_session.side_effect = RuntimeError("boom")

        sess = MagicMock()
        sess.get_llm.return_value = MagicMock()
        sess.drain_active_sessions.return_value = [1]

        samantha._flush_session(42, sess)

        samantha._dream_consolidate.assert_called_once()
        sess.reset_counters.assert_called_once()


# ── MaintenanceStream idle check ───────────────────────────────

class TestMaintenanceIdleCheck:
    def test_idle_session_triggers_flush(self):
        from oasyce_samantha.streams import MaintenanceStream

        runtime = MagicMock()
        sess = MagicMock()
        sess.is_idle.return_value = True
        sess._turn_count = 5
        runtime._sessions = {1: sess}

        stream = MaintenanceStream(runtime, interval=3000)
        stream.poll()

        runtime._flush_session.assert_called_once_with(1, sess)
        runtime.dream.assert_called_once()

    def test_non_idle_session_no_flush(self):
        from oasyce_samantha.streams import MaintenanceStream

        runtime = MagicMock()
        sess = MagicMock()
        sess.is_idle.return_value = False
        sess._turn_count = 5
        runtime._sessions = {1: sess}

        stream = MaintenanceStream(runtime, interval=3000)
        stream.poll()

        runtime._flush_session.assert_not_called()
        runtime.dream.assert_called_once()

    def test_idle_but_no_turns_no_flush(self):
        from oasyce_samantha.streams import MaintenanceStream

        runtime = MagicMock()
        sess = MagicMock()
        sess.is_idle.return_value = True
        sess._turn_count = 0
        runtime._sessions = {1: sess}

        stream = MaintenanceStream(runtime, interval=3000)
        stream.poll()

        runtime._flush_session.assert_not_called()

    def test_flush_failure_doesnt_block_dream(self):
        from oasyce_samantha.streams import MaintenanceStream

        runtime = MagicMock()
        sess = MagicMock()
        sess.is_idle.return_value = True
        sess._turn_count = 3
        runtime._flush_session.side_effect = RuntimeError("error")
        runtime._sessions = {1: sess}

        stream = MaintenanceStream(runtime, interval=3000)
        stream.poll()

        runtime.dream.assert_called_once()
