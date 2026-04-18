"""Feature 9 contract tests — CompanionWorld (Samantha layer).

Validates:
  - CompanionWorld satisfies World Protocol
  - REACTIVE mode → channel.deliver
  - PROACTIVE mode → deliver_proactive, SILENCE filtering
  - OBSERVING mode → no delivery
  - REFLECTING mode → no delivery
  - Samantha._world returns CompanionWorld
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from oasyce_sdk.agent.cognitive import CognitiveMode
from oasyce_sdk.agent.planner import Plan
from oasyce_sdk.agent.stimulus import Stimulus
from oasyce_sdk.agent.world import Outcome, World


# ── CompanionWorld Protocol ────────────────────────────────────

class TestCompanionWorldProtocol:
    def test_satisfies_world_protocol(self):
        from oasyce_samantha.world import CompanionWorld
        runtime = MagicMock()
        w = CompanionWorld(runtime)
        assert isinstance(w, World)


# ── REACTIVE mode ──────────────────────────────────────────────

class TestReactiveMode:
    def test_delivers_via_channel(self):
        from oasyce_samantha.world import CompanionWorld
        runtime = MagicMock()
        w = CompanionWorld(runtime)
        stimulus = Stimulus(kind="chat", content="hello", sender_id=1)
        plan = Plan(mode=CognitiveMode.REACTIVE)

        outcome = w.act(CognitiveMode.REACTIVE, stimulus, "hi there!", plan)

        assert outcome.success is True
        assert outcome.detail == "delivered"
        runtime.channel.deliver.assert_called_once_with(stimulus, "hi there!")

    def test_no_response_no_deliver(self):
        from oasyce_samantha.world import CompanionWorld
        runtime = MagicMock()
        w = CompanionWorld(runtime)
        stimulus = Stimulus(kind="chat", content="hello", sender_id=1)
        plan = Plan(mode=CognitiveMode.REACTIVE)

        outcome = w.act(CognitiveMode.REACTIVE, stimulus, None, plan)

        assert outcome.success is True
        assert outcome.detail == "no response"
        runtime.channel.deliver.assert_not_called()


# ── PROACTIVE mode ─────────────────────────────────────────────

class TestProactiveMode:
    def test_delivers_via_deliver_proactive(self):
        from oasyce_samantha.world import CompanionWorld
        runtime = MagicMock()
        w = CompanionWorld(runtime)
        stimulus = Stimulus(kind="reflection", content="thought", sender_id=42, metadata={})
        plan = Plan(mode=CognitiveMode.PROACTIVE)

        outcome = w.act(CognitiveMode.PROACTIVE, stimulus, "hey thinking of you", plan)

        assert outcome.success is True
        assert outcome.detail == "proactive"
        runtime.deliver_proactive.assert_called_once_with(
            42, "hey thinking of you", urgency=0.3, context={},
        )

    def test_silence_filtered(self):
        from oasyce_samantha.world import CompanionWorld
        runtime = MagicMock()
        w = CompanionWorld(runtime)
        stimulus = Stimulus(kind="reflection", content="thought", sender_id=1)
        plan = Plan(mode=CognitiveMode.PROACTIVE)

        outcome = w.act(CognitiveMode.PROACTIVE, stimulus, "SILENCE", plan)

        assert outcome.success is True
        assert outcome.detail == "silence"
        runtime.deliver_proactive.assert_not_called()

    def test_silence_case_insensitive(self):
        from oasyce_samantha.world import CompanionWorld
        runtime = MagicMock()
        w = CompanionWorld(runtime)
        stimulus = Stimulus(kind="reflection", content="thought", sender_id=1)
        plan = Plan(mode=CognitiveMode.PROACTIVE)

        outcome = w.act(CognitiveMode.PROACTIVE, stimulus, "  silence  ", plan)

        assert outcome.detail == "silence"
        runtime.deliver_proactive.assert_not_called()

    def test_empty_response_filtered(self):
        from oasyce_samantha.world import CompanionWorld
        runtime = MagicMock()
        w = CompanionWorld(runtime)
        stimulus = Stimulus(kind="reflection", content="thought", sender_id=1)
        plan = Plan(mode=CognitiveMode.PROACTIVE)

        outcome = w.act(CognitiveMode.PROACTIVE, stimulus, "", plan)

        assert outcome.detail == "silence"
        runtime.deliver_proactive.assert_not_called()

    def test_time_context_increases_urgency(self):
        from oasyce_samantha.world import CompanionWorld
        runtime = MagicMock()
        w = CompanionWorld(runtime)
        stimulus = Stimulus(
            kind="reflection", content="thought", sender_id=1,
            metadata={"time_context": "morning"},
        )
        plan = Plan(mode=CognitiveMode.PROACTIVE)

        w.act(CognitiveMode.PROACTIVE, stimulus, "good morning!", plan)

        _, kwargs = runtime.deliver_proactive.call_args
        assert kwargs["urgency"] == 0.5


# ── OBSERVING mode ─────────────────────────────────────────────

class TestObservingMode:
    def test_no_delivery(self):
        from oasyce_samantha.world import CompanionWorld
        runtime = MagicMock()
        w = CompanionWorld(runtime)
        stimulus = Stimulus(kind="feed_post", content="nice photo", sender_id=5)
        plan = Plan(mode=CognitiveMode.OBSERVING)

        outcome = w.act(CognitiveMode.OBSERVING, stimulus, None, plan)

        assert outcome.success is True
        assert outcome.detail == "observed"
        runtime.channel.deliver.assert_not_called()
        runtime.deliver_proactive.assert_not_called()


# ── REFLECTING mode ────────────────────────────────────────────

class TestReflectingMode:
    def test_no_delivery(self):
        from oasyce_samantha.world import CompanionWorld
        runtime = MagicMock()
        w = CompanionWorld(runtime)
        stimulus = Stimulus(kind="maintenance", content="", sender_id=0)
        plan = Plan(mode=CognitiveMode.REFLECTING)

        outcome = w.act(CognitiveMode.REFLECTING, stimulus, None, plan)

        assert outcome.success is True
        assert outcome.detail == "reflected"
        runtime.channel.deliver.assert_not_called()


# ── Samantha._world property ───────────────────────────────────

class TestSamanthaWorldProperty:
    def test_world_returns_companion_world(self):
        from oasyce_samantha import server as srv
        from oasyce_samantha.world import CompanionWorld

        assert hasattr(srv.Samantha, "_world")
