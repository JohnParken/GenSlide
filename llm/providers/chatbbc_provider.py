"""
ChatBBC / TL private protocol provider plugin for GenSlide.
"""

import logging
import os
from typing import Any

from llm.base import BaseProvider
from llm.registry import register_provider

logger = logging.getLogger(__name__)


@register_provider("chatbbc", aliases=["tl", "custom"])
class ChatBBCProvider(BaseProvider):
    name = "chatbbc"
    aliases = ["tl", "custom"]
    description = "Corporate chatbbc two-stage RPC/HTTP private protocol"

    def validate_environment(self) -> None:
        base_url = os.getenv("CHATBBC_BASE_URL") or os.getenv("TL_BASE_URL")
        if not base_url:
            raise EnvironmentError(
                "LLM_PROVIDER=chatbbc requires CHATBBC_BASE_URL (or TL_BASE_URL) to be set in .env. "
                "Example: CHATBBC_BASE_URL=http://internal-gateway.company.com"
            )

    def build_llm(self, temperature: float = 0.3, **kwargs: Any) -> Any:
        self.validate_environment()
        from llm.chatbbc_wrapper import ChatBBCChatWrapper

        base_url = (os.getenv("CHATBBC_BASE_URL") or os.getenv("TL_BASE_URL", "")).strip()
        app_id = os.getenv("CHATBBC_APP_ID", "genslide-app").strip()
        tr_code = os.getenv("CHATBBC_TR_CODE", "agent-chat").strip()
        tr_version = os.getenv("CHATBBC_TR_VERSION", "1.0").strip()
        sys_var = os.getenv("CHATBBC_SYS_VAR", "system_prompt").strip()
        auth_token = os.getenv("CHATBBC_AUTH_TOKEN") or os.getenv("TL_AUTH_TOKEN")
        timeout_raw = os.getenv("CHATBBC_TIMEOUT", "150").strip()
        try:
            timeout_seconds = float(timeout_raw)
        except ValueError:
            timeout_seconds = 150.0

        logger.info(
            "LLM provider: ChatBBC private protocol (base_url=%s, app_id=%s, tr_code=%s, temperature=%.1f)",
            base_url,
            app_id,
            tr_code,
            temperature,
        )

        return ChatBBCChatWrapper(
            base_url=base_url,
            app_id=app_id,
            tr_code=tr_code,
            tr_version=tr_version,
            system_variable_name=sys_var,
            auth_token=auth_token,
            timeout_seconds=timeout_seconds,
            temperature=temperature,
            **kwargs,
        )
