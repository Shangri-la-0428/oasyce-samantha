"""Built-in standalone terminal adapter for Samantha."""

from __future__ import annotations

from oasyce_sdk.agent.stimulus import Stimulus

from .base import AdapterCapabilities, AdapterConfig, SurfaceAdapter


class LocalChannel:
    """Minimal stdout delivery sink for local terminal sessions."""

    def deliver(self, stimulus: Stimulus, response: str) -> None:
        if not response:
            return
        print(f"Samantha> {response}")


class LocalAdapter(SurfaceAdapter):
    """Standalone local runtime with a blocking terminal REPL."""

    adapter_id = "local"
    capabilities = AdapterCapabilities(chat=True)

    def __init__(self, config: AdapterConfig) -> None:
        super().__init__(config)
        self._channel = LocalChannel()

    def make_channel(self, runtime):
        return self._channel

    def start(self, runtime) -> None:
        user_id = runtime.config.local_user_id or 1
        session_id = runtime.config.local_session_id or user_id

        print("Samantha local runtime")
        print("Type /exit or /quit to stop.")

        while True:
            try:
                content = input("you> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break

            if not content:
                continue
            if content.lower() in {"/exit", "/quit"}:
                break

            runtime.process(Stimulus(
                kind="chat",
                content=content,
                sender_id=user_id,
                session_id=session_id,
                metadata={"last_message": content},
            ))

    def format_stimulus(self, stimulus: Stimulus) -> str | None:
        return stimulus.content

    def fetch_history(self, runtime, stimulus: Stimulus):
        if stimulus.kind != "chat" or not stimulus.sender_id:
            return []
        from oasyce_sdk.agent.context import ConversationMessage

        session = runtime.session(stimulus.sender_id)
        messages = session.memory.recent_messages(
            session_id=stimulus.session_id or 0,
            limit=20,
        )
        messages.reverse()
        return [
            ConversationMessage(role=m.role, content=m.content)
            for m in messages
        ]

    def deliver_proactive(
        self,
        runtime,
        user_id: int,
        content: str,
        urgency: float = 0.3,
        context: dict | None = None,
    ) -> bool:
        session_id = (context or {}).get("session_id") or runtime.config.local_session_id or user_id
        runtime.channel.deliver(
            Stimulus(
                kind="chat",
                content="",
                sender_id=user_id,
                session_id=session_id,
            ),
            content,
        )
        return True
