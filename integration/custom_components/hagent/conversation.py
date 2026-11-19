"""The conversation entity of each agent that allows Assist (section 15).

The app keeps the conversation history. Each message goes to the app as an
invocation with the source ``assist`` and the HA conversation ID.
"""

from __future__ import annotations

from typing import Literal

from homeassistant.components import conversation
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, Unauthorized
from homeassistant.helpers import intent
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .entity import HagentAgentEntity, HagentConfigEntry, setup_agent_platform
from .invoke import async_check_admin, link_error, result_data
from .link import LinkError
from .runtime import HagentRuntime

# Assist waits for the answer. The app cancels a message after this time.
MESSAGE_TIMEOUT = 120.0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: HagentConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add a conversation entity for each agent that allows Assist."""

    def factory(runtime: HagentRuntime, agent_id: str) -> list[Entity]:
        return [HagentConversationEntity(runtime, agent_id)]

    setup_agent_platform(
        hass,
        entry,
        async_add_entities,
        "conversation",
        factory,
        wanted=lambda agent: bool(agent.get("assist")),
    )


class HagentConversationEntity(HagentAgentEntity, conversation.ConversationEntity):
    """An Assist conversation agent for one hagent agent."""

    _attr_translation_key = "conversation"

    def __init__(self, runtime: HagentRuntime, agent_id: str) -> None:
        super().__init__(runtime, agent_id, "conversation")

    @property
    def supported_languages(self) -> Literal["*"]:
        """The agent answers in any language."""
        return "*"

    async def _async_handle_message(
        self,
        user_input: conversation.ConversationInput,
        chat_log: conversation.ChatLog,
    ) -> conversation.ConversationResult:
        """Send the message to the app and add the answer to the chat log."""
        try:
            await async_check_admin(self.hass, user_input.context)
        except Unauthorized:
            response = intent.IntentResponse(language=user_input.language)
            response.async_set_error(
                intent.IntentResponseErrorCode.UNKNOWN,
                "hagent: only an admin user can talk to this agent",
            )
            return conversation.ConversationResult(
                response=response, conversation_id=chat_log.conversation_id
            )
        try:
            data = await self.runtime.invoke(
                {
                    "agent_id": self.agent_id,
                    "source": "assist",
                    "text": user_input.text,
                    "conversation_id": chat_log.conversation_id,
                    "data": {
                        "language": user_input.language,
                        "device_id": user_input.device_id,
                        "satellite_id": user_input.satellite_id,
                    },
                    "caller": {"user_id": getattr(user_input.context, "user_id", None)},
                    "wait": True,
                    "timeout": MESSAGE_TIMEOUT,
                    "on_timeout": "cancel",
                }
            )
            result = result_data(data)
        except (LinkError, HomeAssistantError) as err:
            error = link_error(err) if isinstance(err, LinkError) else err
            response = intent.IntentResponse(language=user_input.language)
            response.async_set_error(intent.IntentResponseErrorCode.UNKNOWN, str(error))
            return conversation.ConversationResult(
                response=response, conversation_id=chat_log.conversation_id
            )
        text = (
            result
            if isinstance(result, str)
            else ("" if result is None else str(result))
        )
        chat_log.async_add_assistant_content_without_tools(
            conversation.AssistantContent(agent_id=user_input.agent_id, content=text)
        )
        return conversation.async_get_result_from_chat_log(user_input, chat_log)
