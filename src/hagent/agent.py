"""The agent loop: send messages to Claude and run the tools it asks for."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol

from hagent.tools import Toolbox

SYSTEM_PROMPT = """\
You are hagent, an assistant that manages a home through Home Assistant.
Use the tools to look up entities and states before you answer.
Do not guess entity IDs. Find them with list_entities first.
State facts from tool results. If a tool fails, say so.
If you cannot change a device, tell the user what they can do instead."""

MAX_TURNS = 20


class MessagesAPI(Protocol):
    """The part of anthropic.Anthropic().messages that the agent uses."""

    @property
    def create(self) -> Callable[..., Any]: ...


class Agent:
    """Holds one conversation and runs the tool-use loop for each user turn."""

    def __init__(
        self,
        messages_api: MessagesAPI,
        toolbox: Toolbox,
        *,
        model: str,
        max_tokens: int = 4096,
    ) -> None:
        self._api = messages_api
        self._toolbox = toolbox
        self._model = model
        self._max_tokens = max_tokens
        self.history: list[dict[str, Any]] = []

    def ask(self, text: str) -> str:
        """Send one user message. Return the final text reply."""
        self.history.append({"role": "user", "content": text})
        for _ in range(MAX_TURNS):
            response = self._api.create(
                model=self._model,
                max_tokens=self._max_tokens,
                system=SYSTEM_PROMPT,
                tools=self._toolbox.definitions,
                messages=self.history,
            )
            self.history.append({"role": "assistant", "content": response.content})
            if response.stop_reason != "tool_use":
                return "".join(
                    block.text for block in response.content if block.type == "text"
                )
            results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue
                content, is_error = self._toolbox.run(block.name, block.input)
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": content,
                        "is_error": is_error,
                    }
                )
            self.history.append({"role": "user", "content": results})
        return f"Stopped after {MAX_TURNS} tool turns without a final answer."
