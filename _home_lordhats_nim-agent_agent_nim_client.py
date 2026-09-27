"""
NVIDIA NIM API Client.
Wraps the OpenAI-compatible endpoint at integrate.api.nvidia.com
using the official openai SDK. Handles streaming, tool calls, and retries.
"""

import asyncio
import logging
from openai import AsyncOpenAI, APIError, APIConnectionError, RateLimitError

logger = logging.getLogger("nim_agent.nim_client")


class NIMClient:
    """Async client for NVIDIA NIM API (GLM-5)."""

    def __init__(self, api_key: str, base_url: str, model: str,
                 max_tokens: int = 4096, temperature: float = 0.7):
        self.model = model
        self.max_tokens = max_tokens
        self.temperature = temperature
        self._client = AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
        )

    async def chat(self, messages: list[dict], tools: list[dict] | None = None,
                   stream: bool = False) -> dict | None:
        """
        Send a chat completion request.

        Args:
            messages: List of message dicts (role, content).
            tools: Optional list of tool definitions (OpenAI format).
            stream: If True, returns an async generator of chunks.

        Returns:
            The full completion response dict, or async generator if streaming.
        """
        kwargs = {
            "model": self.model,
            "messages": messages,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "stream": stream,
        }
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"

        for attempt in range(3):
            try:
                response = await self._client.chat.completions.create(**kwargs)

                if stream:
                    return response  # async generator

                return response

            except RateLimitError:
                wait = 2 ** (attempt + 1)
                logger.warning(f"Rate limited, retrying in {wait}s...")
                await asyncio.sleep(wait)
            except APIConnectionError as e:
                wait = 2 ** attempt
                logger.warning(f"Connection error: {e}, retrying in {wait}s...")
                await asyncio.sleep(wait)
            except APIError as e:
                logger.error(f"NIM API error: {e}")
                raise

        raise RuntimeError("NIM API: max retries exceeded")

    async def simple_chat(self, user_message: str, system_prompt: str = "") -> str:
        """Quick single-turn chat without tools. Used for self-improvement analysis."""
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": user_message})

        response = await self.chat(messages)
        return response.choices[0].message.content or ""
