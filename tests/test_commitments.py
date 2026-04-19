"""Commitment system contract tests.

Validates:
  - Commitment.matches: topic intersection, kind filter, cadence gate
  - CommitmentSet: CRUD, hot-reload, apply composing into Plan
  - Tool handlers: make/list/withdraw_commitment
  - _quick_annotate: zero-cost topic annotation for chat stimuli
  - _plan integration: commitments applied after rules
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from oasyce_sdk.agent.cognitive import Annotation, CognitiveMode
from oasyce_sdk.agent.planner import Plan
from oasyce_sdk.agent.stimulus import Stimulus


# ── Commitment.matches ────────────────────────────────────────

class TestCommitmentMatches:
    def _make(self, **kw):
        from oasyce_samantha.commitments import Commitment
        defaults = dict(
            name="test",
            topics=["food"],
            instruction="estimate calories",
            tools=[],
            kinds=[],
            cadence="every",
            active=True,
            created_at="2026-04-19T00:00:00Z",
            fired_count=0,
            last_fired_at="",
        )
        defaults.update(kw)
        return Commitment(**defaults)

    def _annotation(self, topics):
        return Annotation(
            target_type="observation",
            target_id=0,
            topics=topics,
        )

    def test_matches_on_topic_intersection(self):
        c = self._make(topics=["food"])
        ann = self._annotation(["food", "mood"])
        assert c.matches(Stimulus(kind="chat", content="hi", sender_id=1), ann)

    def test_no_match_when_topics_disjoint(self):
        c = self._make(topics=["food"])
        ann = self._annotation(["travel", "mood"])
        assert not c.matches(Stimulus(kind="chat", content="hi", sender_id=1), ann)

    def test_no_match_when_annotation_none(self):
        c = self._make(topics=["food"])
        assert not c.matches(Stimulus(kind="chat", content="hi", sender_id=1), None)

    def test_no_match_when_inactive(self):
        c = self._make(active=False)
        ann = self._annotation(["food"])
        assert not c.matches(Stimulus(kind="chat", content="hi", sender_id=1), ann)

    def test_kind_filter_allows(self):
        c = self._make(kinds=["chat"])
        ann = self._annotation(["food"])
        assert c.matches(Stimulus(kind="chat", content="hi", sender_id=1), ann)

    def test_kind_filter_rejects(self):
        c = self._make(kinds=["feed_post"])
        ann = self._annotation(["food"])
        assert not c.matches(Stimulus(kind="chat", content="hi", sender_id=1), ann)

    def test_empty_kinds_matches_all(self):
        c = self._make(kinds=[])
        ann = self._annotation(["food"])
        assert c.matches(Stimulus(kind="feed_post", content="hi", sender_id=1), ann)

    def test_cadence_every_always_matches(self):
        c = self._make(cadence="every", fired_count=100)
        ann = self._annotation(["food"])
        assert c.matches(Stimulus(kind="chat", content="hi", sender_id=1), ann)

    def test_cadence_daily_blocks_same_day(self):
        today = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        c = self._make(cadence="daily", last_fired_at=today)
        ann = self._annotation(["food"])
        assert not c.matches(Stimulus(kind="chat", content="hi", sender_id=1), ann)

    def test_cadence_daily_allows_different_day(self):
        c = self._make(cadence="daily", last_fired_at="2020-01-01T00:00:00Z")
        ann = self._annotation(["food"])
        assert c.matches(Stimulus(kind="chat", content="hi", sender_id=1), ann)

    def test_cadence_daily_allows_never_fired(self):
        c = self._make(cadence="daily", last_fired_at="")
        ann = self._annotation(["food"])
        assert c.matches(Stimulus(kind="chat", content="hi", sender_id=1), ann)

    def test_multiple_topics_any_match(self):
        c = self._make(topics=["food", "fitness"])
        ann = self._annotation(["fitness"])
        assert c.matches(Stimulus(kind="chat", content="hi", sender_id=1), ann)


# ── CommitmentSet CRUD ────────────────────────────────────────

class TestCommitmentSetCRUD:
    def _make_set(self, tmp_path):
        from oasyce_samantha.commitments import CommitmentSet
        path = tmp_path / "commitments.json"
        return CommitmentSet(path=path, commitments=[], mtime=0.0)

    def _make_commitment(self, name="food-coach", topics=None):
        from oasyce_samantha.commitments import Commitment
        return Commitment(
            name=name,
            topics=topics or ["food"],
            instruction="estimate calories",
            tools=[],
            kinds=[],
            cadence="every",
            active=True,
            created_at="2026-04-19T00:00:00Z",
            fired_count=0,
            last_fired_at="",
        )

    def test_add_new(self, tmp_path):
        cs = self._make_set(tmp_path)
        assert cs.add(self._make_commitment()) is True
        assert len(cs) == 1

    def test_add_upsert(self, tmp_path):
        cs = self._make_set(tmp_path)
        cs.add(self._make_commitment())
        cs.add(self._make_commitment())
        assert len(cs) == 1

    def test_get_by_name(self, tmp_path):
        cs = self._make_set(tmp_path)
        cs.add(self._make_commitment("food-coach"))
        assert cs.get("food-coach") is not None
        assert cs.get("nonexistent") is None

    def test_remove_deactivates(self, tmp_path):
        cs = self._make_set(tmp_path)
        cs.add(self._make_commitment("food-coach"))
        assert cs.remove("food-coach") is True
        c = cs.get("food-coach")
        assert c is not None
        assert c.active is False

    def test_remove_nonexistent(self, tmp_path):
        cs = self._make_set(tmp_path)
        assert cs.remove("nonexistent") is False

    def test_save_and_load(self, tmp_path):
        from oasyce_samantha.commitments import CommitmentSet
        cs = self._make_set(tmp_path)
        cs.add(self._make_commitment("food-coach"))
        cs.save()

        loaded = CommitmentSet.load(cs.path)
        assert len(loaded) == 1
        assert loaded.get("food-coach").instruction == "estimate calories"

    def test_load_missing_file(self, tmp_path):
        from oasyce_samantha.commitments import CommitmentSet
        cs = CommitmentSet.load(tmp_path / "missing.json")
        assert len(cs) == 0

    def test_hot_reload(self, tmp_path):
        from oasyce_samantha.commitments import CommitmentSet, Commitment
        path = tmp_path / "commitments.json"
        cs = CommitmentSet(path=path, commitments=[], mtime=0.0)
        cs.add(self._make_commitment("a"))
        cs.save()

        cs2 = CommitmentSet.load(path)
        assert len(cs2) == 1

        # Modify file externally
        data = json.loads(path.read_text())
        data["commitments"].append({
            "name": "b",
            "topics": ["travel"],
            "instruction": "plan trip",
            "cadence": "every",
            "active": True,
            "created_at": "2026-04-19T00:00:00Z",
            "fired_count": 0,
            "last_fired_at": "",
        })
        path.write_text(json.dumps(data))

        # Trigger reload via apply
        ann = Annotation(target_type="observation", target_id=0, topics=["travel"])
        stimulus = Stimulus(kind="chat", content="hi", sender_id=1)
        plan = Plan(mode=CognitiveMode.REACTIVE)
        cs2.apply(stimulus, ann, plan)
        assert len(cs2) == 2

    def test_commitments_property(self, tmp_path):
        cs = self._make_set(tmp_path)
        cs.add(self._make_commitment("a"))
        cs.add(self._make_commitment("b", topics=["travel"]))
        assert len(cs.commitments) == 2


# ── CommitmentSet.apply ───────────────────────────────────────

class TestCommitmentSetApply:
    def _make_set_with_commitment(self, tmp_path, **kw):
        from oasyce_samantha.commitments import Commitment, CommitmentSet
        defaults = dict(
            name="food-coach",
            topics=["food"],
            instruction="estimate calories",
            tools=[],
            kinds=[],
            cadence="every",
            active=True,
            created_at="2026-04-19T00:00:00Z",
            fired_count=0,
            last_fired_at="",
        )
        defaults.update(kw)
        c = Commitment(**defaults)
        cs = CommitmentSet(path=tmp_path / "c.json", commitments=[c], mtime=0.0)
        return cs

    def test_apply_injects_focus(self, tmp_path):
        cs = self._make_set_with_commitment(tmp_path)
        ann = Annotation(target_type="observation", target_id=0, topics=["food"])
        plan = Plan(mode=CognitiveMode.REACTIVE)
        matched = cs.apply(
            Stimulus(kind="chat", content="hi", sender_id=1),
            ann, plan,
        )
        assert "food-coach" in matched
        assert "estimate calories" in plan.focus

    def test_apply_appends_to_existing_focus(self, tmp_path):
        cs = self._make_set_with_commitment(tmp_path)
        ann = Annotation(target_type="observation", target_id=0, topics=["food"])
        plan = Plan(mode=CognitiveMode.REACTIVE, focus="be kind")
        cs.apply(Stimulus(kind="chat", content="hi", sender_id=1), ann, plan)
        assert plan.focus.startswith("be kind")
        assert "estimate calories" in plan.focus

    def test_apply_adds_tools(self, tmp_path):
        cs = self._make_set_with_commitment(tmp_path, tools=["save_memory"])
        ann = Annotation(target_type="observation", target_id=0, topics=["food"])
        plan = Plan(mode=CognitiveMode.REACTIVE, tools=["recall_memory"])
        cs.apply(Stimulus(kind="chat", content="hi", sender_id=1), ann, plan)
        assert "save_memory" in plan.tools

    def test_apply_none_tools_unchanged(self, tmp_path):
        cs = self._make_set_with_commitment(tmp_path, tools=["save_memory"])
        ann = Annotation(target_type="observation", target_id=0, topics=["food"])
        plan = Plan(mode=CognitiveMode.REACTIVE, tools=None)
        cs.apply(Stimulus(kind="chat", content="hi", sender_id=1), ann, plan)
        assert plan.tools is None

    def test_apply_updates_fired_count(self, tmp_path):
        cs = self._make_set_with_commitment(tmp_path)
        ann = Annotation(target_type="observation", target_id=0, topics=["food"])
        cs.apply(Stimulus(kind="chat", content="hi", sender_id=1), ann, Plan(mode=CognitiveMode.REACTIVE))
        c = cs.get("food-coach")
        assert c.fired_count == 1
        assert c.last_fired_at != ""

    def test_apply_no_match_returns_empty(self, tmp_path):
        cs = self._make_set_with_commitment(tmp_path, topics=["food"])
        ann = Annotation(target_type="observation", target_id=0, topics=["travel"])
        plan = Plan(mode=CognitiveMode.REACTIVE)
        matched = cs.apply(Stimulus(kind="chat", content="hi", sender_id=1), ann, plan)
        assert matched == []
        assert not plan.focus

    def test_apply_with_none_annotation(self, tmp_path):
        cs = self._make_set_with_commitment(tmp_path)
        plan = Plan(mode=CognitiveMode.REACTIVE)
        matched = cs.apply(Stimulus(kind="chat", content="hi", sender_id=1), None, plan)
        assert matched == []

    def test_apply_multiple_commitments(self, tmp_path):
        from oasyce_samantha.commitments import Commitment, CommitmentSet
        c1 = Commitment(
            name="food-coach", topics=["food"], instruction="estimate calories",
            tools=[], kinds=[], cadence="every", active=True,
            created_at="2026-04-19T00:00:00Z", fired_count=0, last_fired_at="",
        )
        c2 = Commitment(
            name="fitness-tracker", topics=["fitness"], instruction="log exercise",
            tools=[], kinds=[], cadence="every", active=True,
            created_at="2026-04-19T00:00:00Z", fired_count=0, last_fired_at="",
        )
        cs = CommitmentSet(path=tmp_path / "c.json", commitments=[c1, c2], mtime=0.0)
        ann = Annotation(target_type="observation", target_id=0, topics=["food", "fitness"])
        plan = Plan(mode=CognitiveMode.REACTIVE)
        matched = cs.apply(Stimulus(kind="chat", content="hi", sender_id=1), ann, plan)
        assert len(matched) == 2
        assert "estimate calories" in plan.focus
        assert "log exercise" in plan.focus


# ── Tool handlers ─────────────────────────────────────────────

class TestCommitmentTools:
    def _make_ctx(self, tmp_path):
        from oasyce_samantha.tools import ToolContext
        from oasyce_samantha.commitments import CommitmentSet
        sess = MagicMock()
        sess.commitments = CommitmentSet(
            path=tmp_path / "commitments.json",
            commitments=[],
            mtime=0.0,
        )
        sess.workspace = tmp_path
        ctx = MagicMock(spec=ToolContext)
        ctx.samantha_session = sess
        return ctx

    def test_make_commitment(self, tmp_path):
        from oasyce_samantha.tools import _make_commitment
        ctx = self._make_ctx(tmp_path)
        result = json.loads(_make_commitment({
            "name": "food-coach",
            "topics": ["food"],
            "instruction": "estimate calories",
        }, ctx))
        assert result["saved"] is True
        assert result["name"] == "food-coach"
        assert result["added"] is True

    def test_make_commitment_upsert(self, tmp_path):
        from oasyce_samantha.tools import _make_commitment
        ctx = self._make_ctx(tmp_path)
        _make_commitment({
            "name": "food-coach",
            "topics": ["food"],
            "instruction": "estimate calories",
        }, ctx)
        result = json.loads(_make_commitment({
            "name": "food-coach",
            "topics": ["food", "fitness"],
            "instruction": "estimate calories v2",
        }, ctx))
        assert result["added"] is False

    def test_make_commitment_no_session(self, tmp_path):
        from oasyce_samantha.tools import _make_commitment, ToolContext
        ctx = MagicMock(spec=ToolContext)
        ctx.samantha_session = None
        result = json.loads(_make_commitment({"name": "x", "topics": ["food"], "instruction": "y"}, ctx))
        assert "error" in result

    def test_make_commitment_missing_fields(self, tmp_path):
        from oasyce_samantha.tools import _make_commitment
        ctx = self._make_ctx(tmp_path)
        result = json.loads(_make_commitment({"name": "", "topics": [], "instruction": ""}, ctx))
        assert "error" in result

    def test_list_commitments(self, tmp_path):
        from oasyce_samantha.tools import _make_commitment, _list_commitments
        ctx = self._make_ctx(tmp_path)
        _make_commitment({
            "name": "food-coach",
            "topics": ["food"],
            "instruction": "estimate calories",
        }, ctx)
        result = json.loads(_list_commitments({}, ctx))
        assert len(result) == 1
        assert result[0]["name"] == "food-coach"

    def test_list_commitments_empty(self, tmp_path):
        from oasyce_samantha.tools import _list_commitments
        ctx = self._make_ctx(tmp_path)
        result = json.loads(_list_commitments({}, ctx))
        assert result == []

    def test_withdraw_commitment(self, tmp_path):
        from oasyce_samantha.tools import _make_commitment, _withdraw_commitment
        ctx = self._make_ctx(tmp_path)
        _make_commitment({
            "name": "food-coach",
            "topics": ["food"],
            "instruction": "estimate calories",
        }, ctx)
        result = json.loads(_withdraw_commitment({"name": "food-coach"}, ctx))
        assert result["withdrawn"] is True
        # Commitment still exists but inactive
        c = ctx.samantha_session.commitments.get("food-coach")
        assert c is not None
        assert c.active is False

    def test_withdraw_nonexistent(self, tmp_path):
        from oasyce_samantha.tools import _withdraw_commitment
        ctx = self._make_ctx(tmp_path)
        result = json.loads(_withdraw_commitment({"name": "nope"}, ctx))
        assert result["withdrawn"] is False


# ── _quick_annotate ───────────────────────────────────────────

class TestQuickAnnotate:
    def test_chat_with_food_keywords(self):
        from oasyce_samantha import server as srv
        samantha = MagicMock()
        samantha._quick_annotate = srv.Samantha._quick_annotate.__get__(samantha)
        stimulus = Stimulus(kind="chat", content="今天吃了火锅，好好吃", sender_id=1)
        ann = samantha._quick_annotate(stimulus)
        assert ann is not None
        assert "food" in ann.topics

    def test_chat_with_no_keywords(self):
        from oasyce_samantha import server as srv
        samantha = MagicMock()
        samantha._quick_annotate = srv.Samantha._quick_annotate.__get__(samantha)
        stimulus = Stimulus(kind="chat", content="hello world", sender_id=1)
        ann = samantha._quick_annotate(stimulus)
        assert ann is None

    def test_feed_post_reuses_metadata_annotation(self):
        from oasyce_samantha import server as srv
        samantha = MagicMock()
        samantha._quick_annotate = srv.Samantha._quick_annotate.__get__(samantha)
        ann = Annotation(target_type="observation", target_id=0, topics=["food"])
        stimulus = Stimulus(
            kind="feed_post", content="dinner", sender_id=1,
            metadata={"_annotation": ann},
        )
        result = samantha._quick_annotate(stimulus)
        assert result is ann

    def test_chat_with_multiple_topics(self):
        from oasyce_samantha import server as srv
        samantha = MagicMock()
        samantha._quick_annotate = srv.Samantha._quick_annotate.__get__(samantha)
        stimulus = Stimulus(kind="chat", content="去gym之后吃了烤肉", sender_id=1)
        ann = samantha._quick_annotate(stimulus)
        assert ann is not None
        assert "food" in ann.topics
        assert "fitness" in ann.topics


# ── _plan integration ─────────────────────────────────────────

class TestPlanIntegration:
    def test_commitments_applied_in_plan(self):
        """End-to-end: _quick_annotate detects topic, commitments.apply fires."""
        from oasyce_samantha import server as srv
        from oasyce_samantha.commitments import Commitment, CommitmentSet

        samantha = MagicMock()
        samantha._quick_annotate = srv.Samantha._quick_annotate.__get__(samantha)

        c = Commitment(
            name="food-coach", topics=["food"], instruction="estimate calories",
            tools=[], kinds=[], cadence="every", active=True,
            created_at="2026-04-19T00:00:00Z", fired_count=0, last_fired_at="",
        )
        cs = CommitmentSet(
            path=Path("/tmp/test_c.json"),
            commitments=[c],
            mtime=0.0,
        )

        stimulus = Stimulus(kind="chat", content="今天吃了火锅", sender_id=1)
        ann = samantha._quick_annotate(stimulus)
        assert ann is not None
        assert "food" in ann.topics

        plan = Plan(mode=CognitiveMode.REACTIVE)
        matched = cs.apply(stimulus, ann, plan)
        assert "food-coach" in matched
        assert "estimate calories" in plan.focus

    def test_no_commitments_no_change(self):
        from oasyce_samantha.commitments import CommitmentSet

        cs = CommitmentSet(path=Path("/tmp/test_c.json"), commitments=[], mtime=0.0)
        plan = Plan(mode=CognitiveMode.REACTIVE)
        matched = cs.apply(
            Stimulus(kind="chat", content="hi", sender_id=1),
            None, plan,
        )
        assert matched == []
        assert not plan.focus
