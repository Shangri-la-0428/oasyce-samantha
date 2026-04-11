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
    def _registry():
        from oasyce_samantha.tools import build_default_registry
        return build_default_registry()

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


# ── LLM provider schema (no API call) ────────────────────────────

class TestLLMSchema:
    @staticmethod
    def _defs():
        from oasyce_samantha.tools import build_default_registry
        return build_default_registry().definitions

    def test_tool_defs_are_valid(self):
        for tool in self._defs():
            assert "name" in tool
            assert "description" in tool
            assert "parameters" in tool
            assert tool["parameters"]["type"] == "object"

    def test_new_comment_tools_exist(self):
        names = {t["name"] for t in self._defs()}
        assert "reply_to_comment" in names
        assert "get_post_comments" in names

    def test_reply_to_comment_requires_fields(self):
        reply_tool = next(t for t in self._defs() if t["name"] == "reply_to_comment")
        required = reply_tool["parameters"]["required"]
        assert "post_id" in required
        assert "comment_id" in required
        assert "reply_to_user_id" in required
        assert "content" in required

    def test_config_not_found_raises(self, tmp_path):
        from oasyce_sdk.agent.llm import load_provider

        with pytest.raises(FileNotFoundError):
            load_provider(tmp_path / "nonexistent.json")
