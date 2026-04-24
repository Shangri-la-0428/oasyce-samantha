"""Tests for the Samantha deployment — constitution, tools, Session, tool schemas.

The generic agent-layer concerns (Memory, Context, Planner, Evaluator,
Dream) that used to live here in 0.11.6 now live in
``oasyce-sdk/tests/test_agent_modules.py`` — they cover
``oasyce_sdk.agent.*`` modules that are not Samantha-specific. This
file keeps only what depends on ``oasyce_samantha.*``.
"""

from __future__ import annotations

import json

import pytest


# ── Constitution ──────────────────────────────────────────────────

class TestConstitution:
    def test_creates_default_if_missing(self, tmp_path):
        from oasyce_samantha.constitution import load_constitution

        path = tmp_path / "constitution.md"
        text = load_constitution(path)
        # The default persona is the generic "Samantha" — users override
        # this file with their own name / voice / rules.
        assert "Samantha" in text
        assert path.exists()

    def test_loads_existing(self, tmp_path):
        from oasyce_samantha.constitution import load_constitution

        path = tmp_path / "constitution.md"
        path.write_text("Custom constitution", encoding="utf-8")
        text = load_constitution(path)
        assert text == "Custom constitution"


# ── Tools ─────────────────────────────────────────────────────────

class TestTools:
    @staticmethod
    def _registry(*, include_app: bool = False):
        from oasyce_samantha.tools import build_default_registry

        registry = build_default_registry()
        if include_app:
            from oasyce_samantha.adapters.legacy_app_tools import register_legacy_app_tools

            register_legacy_app_tools(registry)
        return registry

    @staticmethod
    def _ctx(mem):
        from oasyce_samantha.app_client import AppClient
        from oasyce_samantha.tools import ToolContext
        return ToolContext(app=AppClient("http://fake"), memory=mem)

    def test_save_memory_tool(self, tmp_path):
        from oasyce_sdk.agent.memory import Memory

        mem = Memory(db_path=tmp_path / "test.db")
        registry = self._registry()
        ctx = self._ctx(mem)

        result = json.loads(registry.execute("save_memory", {"content": "likes cats", "category": "preference"}, ctx))
        assert result["saved"] is True
        assert mem.count() == 1
        mem.close()

    def test_recall_memory_tool(self, tmp_path):
        from oasyce_sdk.agent.memory import Memory

        mem = Memory(db_path=tmp_path / "test.db")
        mem.save("user loves hiking", "preference")
        registry = self._registry()
        ctx = self._ctx(mem)

        result = json.loads(registry.execute("recall_memory", {"query": "hiking"}, ctx))
        assert len(result) >= 1
        assert "hiking" in result[0]["content"]
        mem.close()

    def test_unknown_tool(self, tmp_path):
        from oasyce_sdk.agent.memory import Memory

        mem = Memory(db_path=tmp_path / "test.db")
        registry = self._registry()
        ctx = self._ctx(mem)

        result = json.loads(registry.execute("nonexistent_tool", {}, ctx))
        assert "error" in result
        mem.close()

    def test_social_writes_are_terminal(self):
        """Write-side social actions must be terminal so the tool loop
        breaks after one call. This is the registry-level guard for
        the multi-reply bug — ``base.py::_generate`` reads ``is_terminal``
        to decide whether to keep looping.
        """
        registry = self._registry(include_app=True)
        assert registry.is_terminal("comment_on_post") is True
        assert registry.is_terminal("reply_to_comment") is True
        assert registry.is_terminal("like_post") is True
        # Read-side tools stay non-terminal so the LLM can chain
        # recall → answer in one turn.
        assert registry.is_terminal("recall_memory") is False
        assert registry.is_terminal("save_memory") is False
        assert registry.is_terminal("get_post_detail") is False

    def test_default_registry_is_surface_agnostic(self):
        registry = self._registry()
        names = {tool["name"] for tool in registry.definitions}
        assert "save_memory" in names
        assert "get_post_detail" not in names
        assert "reply_to_comment" not in names


# ── Session isolation ─────────────────────────────────────────────

class TestSession:
    @staticmethod
    def _fake_registry():
        """Create a minimal ModelRegistry with a fake provider."""
        from oasyce_sdk.agent.llm import LLMResponse, ModelRegistry, ModelSlot

        class FakeLLM:
            def generate(self, messages, tools=None):
                return LLMResponse(text="ok")

        class FakeRegistry(ModelRegistry):
            def __init__(self):
                self._slots = {"fake": ModelSlot(name="fake", provider="openai", api_key="x", model="fake")}
                self._default = "fake"
                self._vision = "fake"
                self._cache = {}
                self._fake = FakeLLM()

            def get(self, *, needs_vision=False):
                return self._fake

        return FakeRegistry()

    def test_per_user_memory_isolation(self, tmp_path, monkeypatch):
        from oasyce_samantha import server as srv
        monkeypatch.setattr(srv, "SAMANTHA_HOME", tmp_path)

        registry = self._fake_registry()
        s1 = srv.Session.load(user_id=1001, registry=registry)
        s2 = srv.Session.load(user_id=1002, registry=registry)

        s1.memory.save("user 1001 likes tea", "preference")
        s2.memory.save("user 1002 likes coffee", "preference")

        assert s1.memory.count() == 1
        assert s2.memory.count() == 1
        assert "tea" in s1.memory.recall("tea")[0].content
        assert "coffee" in s2.memory.recall("coffee")[0].content
        # Cross-isolation: user 1 doesn't see user 2's memory
        assert s1.memory.recall("coffee") == []

        s1.close()
        s2.close()

    def test_per_user_llm_override(self, tmp_path, monkeypatch):
        from oasyce_samantha import server as srv
        monkeypatch.setattr(srv, "SAMANTHA_HOME", tmp_path)

        # Write a per-user LLM config
        user_dir = tmp_path / "users" / "2001"
        user_dir.mkdir(parents=True)
        # Invalid config — should fall back to registry
        (user_dir / "llm.json").write_text('{"provider":"claude","api_key":""}')

        registry = self._fake_registry()
        sess = srv.Session.load(user_id=2001, registry=registry)
        # Should fall back to registry since user config has empty key
        llm = sess.get_llm()
        assert llm is not None  # gets fake from registry
        sess.close()

    def test_session_tracks_active_sessions(self, tmp_path, monkeypatch):
        from oasyce_samantha import server as srv
        monkeypatch.setattr(srv, "SAMANTHA_HOME", tmp_path)

        registry = self._fake_registry()
        sess = srv.Session.load(user_id=3001, registry=registry)
        assert sess._active_session_ids == set()
        sess._active_session_ids.add(42)
        assert 42 in sess._active_session_ids
        sess.close()

    def test_session_has_reflection_timestamp(self, tmp_path, monkeypatch):
        """Activity-gated reflection needs this field on every Session."""
        from oasyce_samantha import server as srv
        monkeypatch.setattr(srv, "SAMANTHA_HOME", tmp_path)

        registry = self._fake_registry()
        sess = srv.Session.load(user_id=3002, registry=registry)
        assert sess._last_reflection_at == 0.0
        assert sess._last_turn_time == 0.0
        sess.close()


class TestSessionLookupCoercion:
    """Prevents the int/str dual-registration bug in production.

    Go backend may deliver sender_id as JSON string while config.user_id
    loads as int. session() must canonicalize so there's one entry per
    relationship.
    """

    def _build_samantha(self, tmp_path, monkeypatch):
        from oasyce_samantha import server as srv
        from oasyce_sdk.agent.llm import LLMResponse, ModelRegistry, ModelSlot

        class FakeLLM:
            def generate(self, messages, tools=None):
                return LLMResponse(text="ok")

        class FakeRegistry(ModelRegistry):
            def __init__(self):
                self._slots = {"fake": ModelSlot(
                    name="fake", provider="openai", api_key="x", model="fake",
                )}
                self._default = "fake"
                self._vision = "fake"
                self._cache = {}
                self._fake = FakeLLM()

            def get(self, *, needs_vision=False):
                return self._fake

        monkeypatch.setattr(srv, "SAMANTHA_HOME", tmp_path)
        return srv, FakeRegistry()

    def test_str_user_id_canonicalizes_to_int(self, tmp_path, monkeypatch):
        srv, registry = self._build_samantha(tmp_path, monkeypatch)
        samantha = object.__new__(srv.Samantha)
        samantha._registry = registry
        import threading
        samantha._sessions = {}
        samantha._sessions_lock = threading.Lock()

        s1 = srv.Samantha.session(samantha, 1776191682194761)
        s2 = srv.Samantha.session(samantha, "1776191682194761")

        # Same relationship — both lookups return the same session object
        assert s1 is s2
        # And only one entry exists, keyed by int
        assert list(samantha._sessions.keys()) == [1776191682194761]
        assert isinstance(list(samantha._sessions.keys())[0], int)
        s1.close()

    def test_falsy_user_id_maps_to_zero(self, tmp_path, monkeypatch):
        srv, registry = self._build_samantha(tmp_path, monkeypatch)
        samantha = object.__new__(srv.Samantha)
        samantha._registry = registry
        import threading
        samantha._sessions = {}
        samantha._sessions_lock = threading.Lock()

        s_none = srv.Samantha.session(samantha, None)
        s_zero = srv.Samantha.session(samantha, 0)
        s_empty = srv.Samantha.session(samantha, "")

        assert s_none is s_zero is s_empty
        assert 0 in samantha._sessions
        s_none.close()


class TestBuildToolCtxBindsAllKinds:
    """`_build_tool_ctx` must bind session for any sender — not just chat.

    Prevents regression of the production bug where reflection/mention/
    comment stimuli hit tools with samantha_session=None, causing every
    core_memory_read / recall_memory call to return `{"error": "no session"}`.
    """

    def _stub_samantha(self, tmp_path, monkeypatch):
        from oasyce_samantha import server as srv
        from oasyce_sdk.agent.llm import LLMResponse, ModelRegistry, ModelSlot

        class FakeLLM:
            def generate(self, messages, tools=None):
                return LLMResponse(text="ok")

        class FakeRegistry(ModelRegistry):
            def __init__(self):
                self._slots = {"fake": ModelSlot(
                    name="fake", provider="openai", api_key="x", model="fake",
                )}
                self._default = "fake"
                self._vision = "fake"
                self._cache = {}
                self._fake = FakeLLM()

            def get(self, *, needs_vision=False):
                return self._fake

        monkeypatch.setattr(srv, "SAMANTHA_HOME", tmp_path)

        import threading
        from unittest.mock import MagicMock

        samantha = object.__new__(srv.Samantha)
        samantha._registry = FakeRegistry()
        samantha._sessions = {}
        samantha._sessions_lock = threading.Lock()
        samantha.surface_adapter = MagicMock(app=object())
        samantha.config = MagicMock(user_id=99)
        samantha.sigil = MagicMock(client=object(), address="addr")
        return srv, samantha

    def test_reflection_stimulus_binds_session(self, tmp_path, monkeypatch):
        srv, samantha = self._stub_samantha(tmp_path, monkeypatch)
        from oasyce_sdk.agent.stimulus import Stimulus
        stim = Stimulus(kind="reflection", content="...", sender_id=555)
        ctx = srv.Samantha._build_tool_ctx(samantha, stim)
        assert ctx.samantha_session is not None
        assert ctx.samantha_session.user_id == 555
        assert ctx.memory is ctx.samantha_session.memory

    def test_chat_stimulus_still_binds_session(self, tmp_path, monkeypatch):
        srv, samantha = self._stub_samantha(tmp_path, monkeypatch)
        from oasyce_sdk.agent.stimulus import Stimulus
        stim = Stimulus(kind="chat", content="hi", sender_id=42)
        ctx = srv.Samantha._build_tool_ctx(samantha, stim)
        assert ctx.samantha_session is not None
        assert ctx.samantha_session.user_id == 42

    def test_mention_stimulus_binds_session(self, tmp_path, monkeypatch):
        srv, samantha = self._stub_samantha(tmp_path, monkeypatch)
        from oasyce_sdk.agent.stimulus import Stimulus
        stim = Stimulus(kind="mention", content="x", sender_id=7)
        ctx = srv.Samantha._build_tool_ctx(samantha, stim)
        assert ctx.samantha_session is not None
        assert ctx.samantha_session.user_id == 7

    def test_no_sender_no_session(self, tmp_path, monkeypatch):
        srv, samantha = self._stub_samantha(tmp_path, monkeypatch)
        from oasyce_sdk.agent.stimulus import Stimulus
        stim = Stimulus(kind="wake", content="tick", sender_id=0)
        ctx = srv.Samantha._build_tool_ctx(samantha, stim)
        assert ctx.samantha_session is None
        assert ctx.memory is None


# ── User rules ────────────────────────────────────────────────────

class TestUserRule:
    def _stimulus(self, kind="chat", content="", sender_id=1):
        from oasyce_sdk.agent.stimulus import Stimulus
        return Stimulus(kind=kind, content=content, sender_id=sender_id)

    def test_keyword_match_case_insensitive(self):
        from oasyce_samantha.rules import UserRule

        rule = UserRule(
            name="food",
            triggers=["吃", "餐"],
            instruction="估算热量",
        )
        assert rule.matches(self._stimulus(content="今天吃了拉面"))
        assert rule.matches(self._stimulus(content="晚餐有沙拉"))
        assert not rule.matches(self._stimulus(content="今天去了图书馆"))

    def test_kind_filter(self):
        from oasyce_samantha.rules import UserRule

        rule = UserRule(
            name="comments-only",
            triggers=["test"],
            instruction="x",
            kinds=["comment"],
        )
        assert rule.matches(self._stimulus(kind="comment", content="test"))
        assert not rule.matches(self._stimulus(kind="chat", content="test"))

    def test_empty_triggers_never_matches(self):
        from oasyce_samantha.rules import UserRule

        rule = UserRule(name="dead", triggers=[], instruction="x")
        assert not rule.matches(self._stimulus(content="anything"))

    def test_from_dict_singular_trigger(self):
        from oasyce_samantha.rules import UserRule

        rule = UserRule.from_dict({
            "name": "single",
            "trigger": "hello",
            "instruction": "say hi",
        })
        assert rule is not None
        assert rule.triggers == ["hello"]

    def test_from_dict_rejects_missing_name(self):
        from oasyce_samantha.rules import UserRule

        assert UserRule.from_dict({"instruction": "x", "triggers": ["a"]}) is None

    def test_from_dict_rejects_missing_instruction(self):
        from oasyce_samantha.rules import UserRule

        assert UserRule.from_dict({"name": "x", "triggers": ["a"]}) is None


class TestRuleSet:
    def _plan(self):
        from oasyce_sdk.agent.planner import Plan
        return Plan()

    def _stimulus(self, content="今天吃了拉面"):
        from oasyce_sdk.agent.stimulus import Stimulus
        return Stimulus(kind="chat", content=content, sender_id=1)

    def _write_rules(self, tmp_path, rules):
        import json
        path = tmp_path / "rules.json"
        path.write_text(json.dumps({"rules": rules}, ensure_ascii=False))
        return path

    def test_missing_file_is_empty_set(self, tmp_path):
        from oasyce_samantha.rules import load_rules

        rs = load_rules(tmp_path)
        assert len(rs) == 0
        # Apply on empty set is a no-op, no crash
        plan = self._plan()
        matched = rs.apply(self._stimulus(), plan)
        assert matched == []
        assert plan.focus == ""

    def test_load_and_apply_basic(self, tmp_path):
        from oasyce_samantha.rules import load_rules

        self._write_rules(tmp_path, [{
            "name": "food-coach",
            "triggers": ["吃", "餐"],
            "instruction": "估算热量并建议下一餐",
        }])
        rs = load_rules(tmp_path)
        assert len(rs) == 1

        plan = self._plan()
        matched = rs.apply(self._stimulus("晚餐吃了沙拉"), plan)
        assert matched == ["food-coach"]
        assert "估算热量" in plan.focus

    def test_focus_concatenates_existing(self, tmp_path):
        from oasyce_samantha.rules import load_rules

        self._write_rules(tmp_path, [{
            "name": "food",
            "triggers": ["吃"],
            "instruction": "估算热量",
        }])
        rs = load_rules(tmp_path)

        plan = self._plan()
        plan.focus = "psyche caution"
        rs.apply(self._stimulus("我吃饭了"), plan)
        assert "psyche caution" in plan.focus
        assert "估算热量" in plan.focus
        assert " | " in plan.focus

    def test_tools_extends_whitelist_without_narrowing(self, tmp_path):
        from oasyce_samantha.rules import load_rules

        self._write_rules(tmp_path, [{
            "name": "food",
            "triggers": ["吃"],
            "instruction": "估算热量",
            "tools": ["save_memory"],
        }])
        rs = load_rules(tmp_path)

        # Existing whitelist gets save_memory added.
        plan = self._plan()
        plan.tools = ["comment_on_post"]
        rs.apply(self._stimulus("吃饭"), plan)
        assert "save_memory" in plan.tools
        assert "comment_on_post" in plan.tools

    def test_tools_does_not_narrow_when_plan_has_none(self, tmp_path):
        from oasyce_samantha.rules import load_rules

        self._write_rules(tmp_path, [{
            "name": "food",
            "triggers": ["吃"],
            "instruction": "x",
            "tools": ["save_memory"],
        }])
        rs = load_rules(tmp_path)

        plan = self._plan()
        plan.tools = None  # all tools — must not be narrowed
        rs.apply(self._stimulus("吃饭"), plan)
        assert plan.tools is None

    def test_multiple_matching_rules_compose(self, tmp_path):
        from oasyce_samantha.rules import load_rules

        self._write_rules(tmp_path, [
            {"name": "a", "triggers": ["吃"], "instruction": "热量"},
            {"name": "b", "triggers": ["拉面"], "instruction": "营养"},
        ])
        rs = load_rules(tmp_path)

        plan = self._plan()
        matched = rs.apply(self._stimulus("吃拉面"), plan)
        assert matched == ["a", "b"]
        assert "热量" in plan.focus
        assert "营养" in plan.focus

    def test_malformed_json_does_not_crash(self, tmp_path):
        from oasyce_samantha.rules import load_rules

        (tmp_path / "rules.json").write_text("{not valid json")
        rs = load_rules(tmp_path)
        assert len(rs) == 0  # falls back to empty

    def test_hot_reload_on_mtime_change(self, tmp_path):
        import os
        import time

        from oasyce_samantha.rules import load_rules

        self._write_rules(tmp_path, [{
            "name": "v1", "triggers": ["a"], "instruction": "first",
        }])
        rs = load_rules(tmp_path)
        assert len(rs) == 1
        assert rs.rules[0].name == "v1"

        # Rewrite with a new rule and bump mtime
        time.sleep(0.01)
        path = tmp_path / "rules.json"
        new_mtime = path.stat().st_mtime + 1
        self._write_rules(tmp_path, [{
            "name": "v2", "triggers": ["b"], "instruction": "second",
        }])
        os.utime(path, (new_mtime, new_mtime))

        # apply() triggers reload
        plan = self._plan()
        rs.apply(self._stimulus("b"), plan)
        assert any(r.name == "v2" for r in rs.rules)

    def test_kinds_filter_in_apply(self, tmp_path):
        from oasyce_sdk.agent.stimulus import Stimulus

        from oasyce_samantha.rules import load_rules

        self._write_rules(tmp_path, [{
            "name": "comment-only",
            "triggers": ["test"],
            "instruction": "x",
            "kinds": ["comment"],
        }])
        rs = load_rules(tmp_path)

        plan = self._plan()
        rs.apply(Stimulus(kind="chat", content="test", sender_id=1), plan)
        assert plan.focus == ""  # not matched — wrong kind

        plan = self._plan()
        rs.apply(Stimulus(kind="comment", content="test", sender_id=1), plan)
        assert plan.focus == "x"


class TestSessionLoadsRules:
    def test_session_loads_rules_from_workspace(self, tmp_path, monkeypatch):
        import json

        from oasyce_samantha import server as srv
        monkeypatch.setattr(srv, "SAMANTHA_HOME", tmp_path)

        user_dir = tmp_path / "users" / "9001"
        user_dir.mkdir(parents=True)
        (user_dir / "rules.json").write_text(json.dumps({"rules": [{
            "name": "food",
            "triggers": ["吃"],
            "instruction": "估算热量",
        }]}))

        from oasyce_sdk.agent.llm import LLMResponse, ModelRegistry, ModelSlot

        class FakeRegistry(ModelRegistry):
            def __init__(self):
                self._slots = {"f": ModelSlot(name="f", provider="openai", api_key="x", model="f")}
                self._default = "f"
                self._vision = "f"
                self._cache = {}

            def get(self, *, needs_vision=False):
                class _LLM:
                    def generate(self, messages, tools=None):
                        return LLMResponse(text="ok")
                return _LLM()

        sess = srv.Session.load(user_id=9001, registry=FakeRegistry())
        assert len(sess.rules) == 1
        assert sess.rules.rules[0].name == "food"
        sess.close()


# ── RuleSet CRUD ──────────────────────────────────────────────────

class TestRuleSetCRUD:
    def test_add_new_rule_returns_true(self, tmp_path):
        from oasyce_samantha.rules import RuleSet, UserRule

        rs = RuleSet.load(tmp_path / "rules.json")
        added = rs.add(UserRule(name="x", triggers=["a"], instruction="do"))
        assert added is True
        assert len(rs) == 1

    def test_add_upserts_existing_returns_false(self, tmp_path):
        from oasyce_samantha.rules import RuleSet, UserRule

        rs = RuleSet.load(tmp_path / "rules.json")
        rs.add(UserRule(name="x", triggers=["a"], instruction="first"))
        added = rs.add(UserRule(name="x", triggers=["a"], instruction="second"))
        assert added is False
        assert len(rs) == 1
        assert rs.get("x").instruction == "second"

    def test_remove_existing_returns_true(self, tmp_path):
        from oasyce_samantha.rules import RuleSet, UserRule

        rs = RuleSet.load(tmp_path / "rules.json")
        rs.add(UserRule(name="x", triggers=["a"], instruction="do"))
        assert rs.remove("x") is True
        assert len(rs) == 0

    def test_remove_missing_returns_false(self, tmp_path):
        from oasyce_samantha.rules import RuleSet

        rs = RuleSet.load(tmp_path / "rules.json")
        assert rs.remove("ghost") is False

    def test_save_roundtrip_preserves_fields(self, tmp_path):
        from oasyce_samantha.rules import RuleSet, UserRule

        path = tmp_path / "rules.json"
        rs = RuleSet.load(path)
        rs.add(UserRule(
            name="food", triggers=["吃", "餐"], instruction="估算热量",
            tools=["save_memory"], kinds=["chat"],
        ))
        rs.save()

        assert path.exists()
        # Load fresh — same data survives the round-trip
        rs2 = RuleSet.load(path)
        assert len(rs2) == 1
        r = rs2.get("food")
        assert r.triggers == ["吃", "餐"]
        assert r.instruction == "估算热量"
        assert r.tools == ["save_memory"]
        assert r.kinds == ["chat"]

    def test_save_creates_parent_dirs(self, tmp_path):
        from oasyce_samantha.rules import RuleSet, UserRule

        path = tmp_path / "nested" / "path" / "rules.json"
        rs = RuleSet.load(path)
        rs.add(UserRule(name="x", triggers=["a"], instruction="i"))
        rs.save()
        assert path.exists()

    def test_to_dict_omits_empty_optional_fields(self):
        from oasyce_samantha.rules import UserRule

        r = UserRule(name="x", triggers=["a"], instruction="i")
        d = r.to_dict()
        assert d == {"name": "x", "triggers": ["a"], "instruction": "i"}
        assert "tools" not in d
        assert "kinds" not in d

    def test_to_dict_includes_populated_optional_fields(self):
        from oasyce_samantha.rules import UserRule

        r = UserRule(
            name="x", triggers=["a"], instruction="i",
            tools=["save_memory"], kinds=["chat"],
        )
        d = r.to_dict()
        assert d["tools"] == ["save_memory"]
        assert d["kinds"] == ["chat"]


# ── Standing rule chat tools ──────────────────────────────────────

class TestStandingRuleTools:
    @staticmethod
    def _registry():
        from oasyce_samantha.tools import build_default_registry
        return build_default_registry()

    @staticmethod
    def _ctx(tmp_path):
        """Build a ToolContext with a stub session exposing rules.

        The three chat tools only touch ``ctx.samantha_session.rules``,
        so a minimal stub with a loaded ``RuleSet`` is enough — we
        don't need a full ``Session`` here.
        """
        from oasyce_samantha.app_client import AppClient
        from oasyce_samantha.rules import RuleSet
        from oasyce_samantha.tools import ToolContext
        from oasyce_sdk.agent.memory import Memory

        class StubSession:
            def __init__(self, path):
                self.rules = RuleSet.load(path)

        mem = Memory(db_path=tmp_path / "mem.db")
        session = StubSession(tmp_path / "rules.json")
        ctx = ToolContext(
            app=AppClient("http://fake"),
            memory=mem,
            samantha_session=session,
        )
        return ctx, session, mem

    def test_add_standing_rule_persists(self, tmp_path):
        registry = self._registry()
        ctx, session, mem = self._ctx(tmp_path)

        result = json.loads(registry.execute("add_standing_rule", {
            "name": "food-coach",
            "triggers": ["吃", "餐"],
            "instruction": "估算热量并建议下一餐",
            "tools": ["save_memory"],
        }, ctx))

        assert result["saved"] is True
        assert result["added"] is True
        assert result["total_rules"] == 1
        assert (tmp_path / "rules.json").exists()
        assert session.rules.get("food-coach").instruction == "估算热量并建议下一餐"
        mem.close()

    def test_add_standing_rule_upserts(self, tmp_path):
        registry = self._registry()
        ctx, session, mem = self._ctx(tmp_path)

        registry.execute("add_standing_rule", {
            "name": "food-coach",
            "triggers": ["吃"],
            "instruction": "v1",
        }, ctx)
        result = json.loads(registry.execute("add_standing_rule", {
            "name": "food-coach",
            "triggers": ["吃"],
            "instruction": "v2",
        }, ctx))

        assert result["added"] is False  # replaced, not new
        assert session.rules.get("food-coach").instruction == "v2"
        mem.close()

    def test_standing_rule_tools_are_not_terminal(self):
        """After adding/listing/removing a rule, the LLM still owes the
        user a natural-language confirmation, so these tools must NOT
        break the tool loop. Non-terminal is how read tools behave too.
        """
        registry = self._registry()
        assert registry.is_terminal("add_standing_rule") is False
        assert registry.is_terminal("list_standing_rules") is False
        assert registry.is_terminal("remove_standing_rule") is False

    def test_add_standing_rule_rejects_empty_triggers(self, tmp_path):
        registry = self._registry()
        ctx, _, mem = self._ctx(tmp_path)

        result = json.loads(registry.execute("add_standing_rule", {
            "name": "x",
            "triggers": [],
            "instruction": "y",
        }, ctx))
        assert "error" in result
        mem.close()

    def test_add_standing_rule_accepts_string_trigger(self, tmp_path):
        """Tolerate a common LLM mistake: sending a string instead of
        a one-element list. The handler coerces before building the rule.
        """
        registry = self._registry()
        ctx, session, mem = self._ctx(tmp_path)

        result = json.loads(registry.execute("add_standing_rule", {
            "name": "x",
            "triggers": "hello",
            "instruction": "y",
        }, ctx))
        assert result["saved"] is True
        assert session.rules.get("x").triggers == ["hello"]
        mem.close()

    def test_list_standing_rules(self, tmp_path):
        registry = self._registry()
        ctx, _, mem = self._ctx(tmp_path)

        # Empty at first
        result = json.loads(registry.execute("list_standing_rules", {}, ctx))
        assert result == []

        # Add then list
        registry.execute("add_standing_rule", {
            "name": "food",
            "triggers": ["吃"],
            "instruction": "热量",
        }, ctx)
        result = json.loads(registry.execute("list_standing_rules", {}, ctx))
        assert len(result) == 1
        assert result[0]["name"] == "food"
        assert result[0]["triggers"] == ["吃"]
        mem.close()

    def test_remove_standing_rule(self, tmp_path):
        registry = self._registry()
        ctx, session, mem = self._ctx(tmp_path)

        registry.execute("add_standing_rule", {
            "name": "food",
            "triggers": ["吃"],
            "instruction": "热量",
        }, ctx)
        result = json.loads(registry.execute("remove_standing_rule", {"name": "food"}, ctx))

        assert result["removed"] is True
        assert result["total_rules"] == 0
        assert len(session.rules) == 0
        mem.close()

    def test_remove_missing_rule_returns_false(self, tmp_path):
        registry = self._registry()
        ctx, _, mem = self._ctx(tmp_path)

        result = json.loads(registry.execute("remove_standing_rule", {"name": "ghost"}, ctx))
        assert result["removed"] is False
        mem.close()

    def test_no_session_returns_error(self, tmp_path):
        """Safe-by-default: calling a rule tool without a chat session
        (e.g. via the HTTP endpoint before the user is resolved) must
        return a structured error, not crash with AttributeError."""
        from oasyce_samantha.app_client import AppClient
        from oasyce_samantha.tools import ToolContext
        from oasyce_sdk.agent.memory import Memory

        mem = Memory(db_path=tmp_path / "mem.db")
        ctx = ToolContext(app=AppClient("http://fake"), memory=mem)  # samantha_session=None
        registry = self._registry()

        result = json.loads(registry.execute("add_standing_rule", {
            "name": "x", "triggers": ["a"], "instruction": "i",
        }, ctx))
        assert "error" in result
        mem.close()


# ── LLM provider schema (no API call) ────────────────────────────

class TestLLMSchema:
    @staticmethod
    def _defs(*, include_app: bool = False):
        from oasyce_samantha.tools import build_default_registry

        registry = build_default_registry()
        if include_app:
            from oasyce_samantha.adapters.legacy_app_tools import register_legacy_app_tools

            register_legacy_app_tools(registry)
        return registry.definitions

    def test_tool_defs_are_valid(self):
        for tool in self._defs():
            assert "name" in tool
            assert "description" in tool
            assert "parameters" in tool
            assert tool["parameters"]["type"] == "object"

    def test_new_comment_tools_exist(self):
        names = {t["name"] for t in self._defs(include_app=True)}
        assert "reply_to_comment" in names
        assert "get_post_comments" in names

    def test_reply_to_comment_requires_fields(self):
        reply_tool = next(t for t in self._defs(include_app=True) if t["name"] == "reply_to_comment")
        required = reply_tool["parameters"]["required"]
        assert "post_id" in required
        assert "comment_id" in required
        assert "reply_to_user_id" in required
        assert "content" in required

    def test_standing_rule_tools_exist(self):
        names = {t["name"] for t in self._defs()}
        assert "add_standing_rule" in names
        assert "list_standing_rules" in names
        assert "remove_standing_rule" in names

    def test_add_standing_rule_required_fields(self):
        tool = next(t for t in self._defs() if t["name"] == "add_standing_rule")
        required = tool["parameters"]["required"]
        assert "name" in required
        assert "triggers" in required
        assert "instruction" in required

    def test_remove_standing_rule_requires_name(self):
        tool = next(t for t in self._defs() if t["name"] == "remove_standing_rule")
        assert "name" in tool["parameters"]["required"]

    def test_config_not_found_raises(self, tmp_path):
        from oasyce_sdk.agent.llm import load_provider

        with pytest.raises(FileNotFoundError):
            load_provider(tmp_path / "nonexistent.json")
