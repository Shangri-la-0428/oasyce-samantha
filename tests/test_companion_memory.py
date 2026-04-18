"""Contract tests for CompanionMemory — unified facade over all stores.

Validates retrieval with closet boost, essential story, observation
integration, knowledge integration, and backward-compatible Session wiring.
"""

from __future__ import annotations

import threading

import pytest


class TestCompanionMemory:
    def test_construction_creates_all_stores(self, tmp_path):
        from oasyce_samantha.memory import CompanionMemory

        mem = CompanionMemory(tmp_path)
        assert mem.episodic is not None
        assert mem.observations is not None
        assert mem.knowledge is not None
        assert mem.core is not None
        assert mem.summaries is not None
        mem.close()

    def test_shared_db_file(self, tmp_path):
        from oasyce_samantha.memory import CompanionMemory
        from oasyce_sdk.agent.cognitive import KnowledgeTriple, Observation

        mem = CompanionMemory(tmp_path)
        mem.episodic.save("user likes tea", "preference")
        mem.observations.save_observation(Observation(
            source_type="post", content="Mountain trail review",
        ))
        mem.knowledge.add_triple(KnowledgeTriple(
            subject="user", predicate="likes", object="tea",
        ))

        assert mem.episodic.count() == 1
        assert len(mem.observations.search_observations("mountain")) >= 1
        assert len(mem.knowledge.search_triples("tea")) >= 1
        mem.close()


class TestRetrieval:
    def test_facts_only(self, tmp_path):
        from oasyce_samantha.memory import CompanionMemory

        mem = CompanionMemory(tmp_path)
        mem.episodic.save("user enjoys snow mountain hiking", "preference")

        results = mem.retrieve("snow mountain")
        assert len(results) >= 1
        assert results[0]["type"] == "fact"
        assert "snow" in results[0]["content"].lower()
        mem.close()

    def test_observations_included(self, tmp_path):
        from oasyce_samantha.memory import CompanionMemory
        from oasyce_sdk.agent.cognitive import Observation

        mem = CompanionMemory(tmp_path)
        mem.observations.save_observation(Observation(
            source_type="post",
            content="Beautiful photos from Jade Dragon Snow Mountain",
            location="Lijiang",
        ))

        results = mem.retrieve("snow mountain")
        assert len(results) >= 1
        obs = [r for r in results if r["type"] == "observation"]
        assert len(obs) >= 1
        assert "Jade Dragon" in obs[0]["content"]
        mem.close()

    def test_closet_boost(self, tmp_path):
        from oasyce_samantha.memory import CompanionMemory
        from oasyce_sdk.agent.cognitive import Annotation, Observation

        mem = CompanionMemory(tmp_path)
        obs_id = mem.observations.save_observation(Observation(
            source_type="post",
            content="Weekend trip to Sichuan province",
            location="Chengdu",
        ))
        mem.observations.save_annotation(Annotation(
            target_type="observation", target_id=obs_id,
            topics=["travel"],
            summary="Great winter scenery snow mountain recommendations",
        ))

        results = mem.retrieve("snow mountain")
        obs = [r for r in results if r["type"] == "observation"]
        assert len(obs) >= 1
        # Annotation-surfaced observation: base 0.5 + 0.3 boost > 0.5 alone
        assert obs[0]["score"] > 0.5
        assert "Sichuan" in obs[0]["content"]

        mem.close()

    def test_emotional_weight_boost(self, tmp_path):
        from oasyce_samantha.memory import CompanionMemory
        from oasyce_sdk.agent.cognitive import Observation

        mem = CompanionMemory(tmp_path)
        mem.observations.save_observation(Observation(
            source_type="post",
            content="Snow scenery in Tibet low emotional weight",
            emotional_weight=0.1,
        ))
        mem.observations.save_observation(Observation(
            source_type="post",
            content="Snow scenery in Yunnan high emotional weight",
            emotional_weight=0.9,
        ))

        results = mem.retrieve("snow scenery")
        assert len(results) >= 2
        yunnan = [r for r in results if "Yunnan" in r["content"]]
        tibet = [r for r in results if "Tibet" in r["content"]]
        assert yunnan and tibet
        assert yunnan[0]["score"] > tibet[0]["score"]
        mem.close()

    def test_mixed_facts_and_observations(self, tmp_path):
        from oasyce_samantha.memory import CompanionMemory
        from oasyce_sdk.agent.cognitive import Observation

        mem = CompanionMemory(tmp_path)
        mem.episodic.save("user asked about snow mountain scenery", "topic")
        mem.observations.save_observation(Observation(
            source_type="post",
            content="Visited Jade Dragon Snow Mountain last spring",
        ))

        results = mem.retrieve("snow mountain", limit=5)
        types = {r["type"] for r in results}
        assert "fact" in types
        assert "observation" in types
        mem.close()

    def test_empty_query(self, tmp_path):
        from oasyce_samantha.memory import CompanionMemory

        mem = CompanionMemory(tmp_path)
        results = mem.retrieve("")
        assert results == []
        mem.close()


class TestIntegration:
    def test_integrate_observation(self, tmp_path):
        from oasyce_samantha.memory import CompanionMemory
        from oasyce_sdk.agent.cognitive import Annotation, Observation

        mem = CompanionMemory(tmp_path)
        obs = Observation(
            source_type="post", source_id=42,
            content="Snow scenery at Jade Dragon Mountain",
            location="Lijiang",
        )
        ann = Annotation(
            topics=["travel", "mountain"],
            entities=["Jade Dragon Snow Mountain"],
            sentiment="positive",
            summary="Beautiful snow mountain scenery",
        )
        obs_id = mem.integrate_observation(obs, ann)
        assert obs_id >= 1

        row = mem.observations.get_observation(obs_id)
        assert row is not None
        assert row.content == obs.content

        anns = mem.observations.get_annotations_for("observation", obs_id)
        assert len(anns) == 1
        assert anns[0].topics == ["travel", "mountain"]
        mem.close()

    def test_integrate_observation_without_annotation(self, tmp_path):
        from oasyce_samantha.memory import CompanionMemory
        from oasyce_sdk.agent.cognitive import Observation

        mem = CompanionMemory(tmp_path)
        obs_id = mem.integrate_observation(Observation(
            source_type="post", content="Just a quick post",
        ))
        assert obs_id >= 1
        assert mem.observations.get_annotations_for("observation", obs_id) == []
        mem.close()

    def test_integrate_knowledge(self, tmp_path):
        from oasyce_samantha.memory import CompanionMemory
        from oasyce_sdk.agent.cognitive import KnowledgeTriple

        mem = CompanionMemory(tmp_path)
        tid = mem.integrate_knowledge(KnowledgeTriple(
            subject="Jade Dragon Snow Mountain",
            predicate="located_in",
            object="Lijiang, Yunnan",
        ))
        assert tid >= 1

        results = mem.knowledge.find_by_entity("Jade Dragon")
        assert len(results) >= 1
        mem.close()


class TestEssentialStory:
    def test_empty_when_not_generated(self, tmp_path):
        from oasyce_samantha.memory import CompanionMemory

        mem = CompanionMemory(tmp_path)
        assert mem.essential_story() == ""
        mem.close()

    def test_save_and_load(self, tmp_path):
        from oasyce_samantha.memory import CompanionMemory

        mem = CompanionMemory(tmp_path)
        mem.save_essential_story("This person loves hiking and snow mountains.")

        mem2 = CompanionMemory(tmp_path)
        assert mem2.essential_story() == "This person loves hiking and snow mountains."
        mem.close()
        mem2.close()

    def test_file_persists(self, tmp_path):
        from oasyce_samantha.memory import CompanionMemory

        mem = CompanionMemory(tmp_path)
        mem.save_essential_story("test story")
        mem.close()

        assert (tmp_path / "essential_story.txt").exists()
        assert (tmp_path / "essential_story.txt").read_text() == "test story"


class TestCoreMemoryDelegation:
    def test_update_persists(self, tmp_path):
        from oasyce_samantha.memory import CompanionMemory

        mem = CompanionMemory(tmp_path)
        stored = mem.update_core_memory("human", "likes tea and hiking")
        assert stored == "likes tea and hiking"
        assert mem.core.get("human") == "likes tea and hiking"

        mem2 = CompanionMemory(tmp_path)
        assert mem2.core.get("human") == "likes tea and hiking"
        mem.close()
        mem2.close()


class TestSessionBackwardCompat:
    @staticmethod
    def _fake_registry():
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

    def test_session_has_all_stores(self, tmp_path, monkeypatch):
        from oasyce_samantha import server as srv
        monkeypatch.setattr(srv, "SAMANTHA_HOME", tmp_path)

        sess = srv.Session.load(user_id=1001, registry=self._fake_registry())
        assert sess.memory is not None
        assert sess.core_memory is not None
        assert sess.history_summary is not None
        assert sess.observation_store is not None
        assert sess.knowledge_store is not None
        sess.close()

    def test_memory_isolation_preserved(self, tmp_path, monkeypatch):
        from oasyce_samantha import server as srv
        monkeypatch.setattr(srv, "SAMANTHA_HOME", tmp_path)

        reg = self._fake_registry()
        s1 = srv.Session.load(user_id=1001, registry=reg)
        s2 = srv.Session.load(user_id=1002, registry=reg)

        s1.memory.save("user 1001 likes tea", "preference")
        s2.memory.save("user 1002 likes coffee", "preference")

        assert s1.memory.count() == 1
        assert s2.memory.count() == 1
        assert s1.memory.recall("coffee") == []

        s1.close()
        s2.close()

    def test_observation_store_works_via_session(self, tmp_path, monkeypatch):
        from oasyce_sdk.agent.cognitive import Observation

        from oasyce_samantha import server as srv
        monkeypatch.setattr(srv, "SAMANTHA_HOME", tmp_path)

        sess = srv.Session.load(user_id=2001, registry=self._fake_registry())
        sess.observation_store.save_observation(Observation(
            source_type="post", content="Snow mountain photo",
        ))

        results = sess.observation_store.search_observations("snow")
        assert len(results) >= 1
        sess.close()

    def test_update_core_memory_via_session(self, tmp_path, monkeypatch):
        from oasyce_samantha import server as srv
        monkeypatch.setattr(srv, "SAMANTHA_HOME", tmp_path)

        sess = srv.Session.load(user_id=3001, registry=self._fake_registry())
        stored = sess.update_core_memory("human", "likes snow mountains")
        assert stored == "likes snow mountains"
        assert sess.core_memory.get("human") == "likes snow mountains"
        sess.close()
