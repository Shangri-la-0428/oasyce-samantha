from __future__ import annotations

from types import SimpleNamespace

from oasyce_sdk.agent.stimulus import Stimulus


class TestAdapterConfig:
    def test_explicit_local_config_stays_local(self):
        from oasyce_samantha.server import SamanthaConfig
        from oasyce_samantha.adapters import AdapterConfig

        config = SamanthaConfig(adapter="local")
        adapter = AdapterConfig.from_runtime_config(config)
        assert adapter.name == "local"

    def test_legacy_app_config_is_inferred(self):
        from oasyce_samantha.server import SamanthaConfig
        from oasyce_samantha.adapters import AdapterConfig

        config = SamanthaConfig(
            app_api_base="http://127.0.0.1:8080/api/v1",
            jwt_token="token",
            user_id=42,
        )
        adapter = AdapterConfig.from_runtime_config(config)
        assert adapter.name == "app-legacy"
        assert adapter.options["app_api_base"] == "http://127.0.0.1:8080/api/v1"
        assert adapter.options["jwt_token"] == "token"
        assert adapter.options["user_id"] == 42


class TestAdapterLoader:
    def test_loads_builtin_local_adapter(self):
        from oasyce_samantha.adapters import AdapterConfig, AdapterLoader
        from oasyce_samantha.adapters.local import LocalAdapter

        adapter = AdapterLoader.load(AdapterConfig(name="local"))
        assert isinstance(adapter, LocalAdapter)
        assert adapter.capabilities.chat is True
        assert adapter.capabilities.social_feed is False

    def test_loads_builtin_legacy_app_adapter(self):
        from oasyce_samantha.adapters import AdapterConfig, AdapterLoader
        from oasyce_samantha.adapters.legacy_app import LegacyAppAdapter

        adapter = AdapterLoader.load(AdapterConfig(
            name="app-legacy",
            options={"app_api_base": "http://127.0.0.1:8080/api/v1", "jwt_token": "x"},
        ))
        assert isinstance(adapter, LegacyAppAdapter)
        assert adapter.capabilities.social_feed is True
        assert adapter.capabilities.public_posting is True


class TestLocalAdapter:
    def test_local_channel_prints_responses(self, capsys):
        from oasyce_samantha.adapters.local import LocalChannel

        channel = LocalChannel()
        channel.deliver(Stimulus(kind="chat", content="hi", session_id=1), "hello")
        out = capsys.readouterr().out
        assert "Samantha> hello" in out

    def test_local_fetch_history_uses_session_memory(self, tmp_path, monkeypatch):
        from oasyce_sdk.agent.llm import LLMResponse
        from oasyce_samantha.adapters import AdapterConfig
        from oasyce_samantha.adapters.local import LocalAdapter
        from oasyce_samantha import server as srv

        class FakeRegistry:
            slot_names = ["fake"]
            default_name = "fake"

            def get(self, *, needs_vision: bool = False):
                class FakeLLM:
                    def generate(self, messages, tools=None):
                        return LLMResponse(text="ok")

                return FakeLLM()

        monkeypatch.setattr(srv, "SAMANTHA_HOME", tmp_path)

        class Runtime:
            config = type("Cfg", (), {"local_session_id": 1})()

            def __init__(self):
                self._session = srv.Session.load(1, FakeRegistry())

            def session(self, user_id: int):
                return self._session

        runtime = Runtime()
        runtime.session(1).memory.log_message("user", "hello", session_id=1)
        runtime.session(1).memory.log_message("assistant", "hi there", session_id=1)

        adapter = LocalAdapter(AdapterConfig(name="local"))
        history = adapter.fetch_history(
            runtime,
            Stimulus(kind="chat", content="hello", sender_id=1, session_id=1),
        )

        assert [m.role for m in history] == ["user", "assistant"]
        assert [m.content for m in history] == ["hello", "hi there"]
        runtime.session(1).close()


class TestLegacyAppSurfaceHelpers:
    def test_format_legacy_app_mention_prompt_keeps_social_hint(self):
        from oasyce_samantha.adapters.legacy_app_surface import format_legacy_app_stimulus

        prompt = format_legacy_app_stimulus(Stimulus(
            kind="mention",
            content="hey @samantha",
            sender_id=9,
            post_id=77,
            metadata={
                "post_author": "alice",
                "post_title": "sunset",
                "post_content": "look at this",
                "post_location": "shanghai",
            },
        ))

        assert "Someone mentioned you in a post." in prompt
        assert "comment_on_post(post_id=77)" in prompt

    def test_inject_legacy_app_tool_defaults_prefills_ids(self):
        from oasyce_samantha.adapters.legacy_app_surface import inject_legacy_app_tool_defaults

        tool_call = SimpleNamespace(arguments={})
        stimulus = Stimulus(
            kind="comment",
            content="nice",
            sender_id=5,
            post_id=11,
            comment_id=12,
            metadata={"root_id": 12},
        )

        inject_legacy_app_tool_defaults(tool_call, stimulus)

        assert tool_call.arguments["post_id"] == 11
        assert tool_call.arguments["comment_id"] == 12
        assert tool_call.arguments["reply_to_user_id"] == 5
        assert tool_call.arguments["root_id"] == 12
