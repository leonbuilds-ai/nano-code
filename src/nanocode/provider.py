"""Small OpenAI-compatible streaming client."""

import json
import httpx


class Provider:
    def __init__(self, base_url: str, api_key: str, model: str):
        self.url = base_url.rstrip("/") + "/chat/completions"
        self.api_key = api_key
        self.model = model

    async def stream(self, messages: list[dict], tools: list[dict]):
        payload = {"model": self.model, "messages": messages, "tools": tools, "stream": True}
        headers = {"Authorization": f"Bearer {self.api_key}"}
        async with httpx.AsyncClient(timeout=httpx.Timeout(120, connect=10), trust_env=False) as client:
            async with client.stream("POST", self.url, json=payload, headers=headers) as response:
                response.raise_for_status()
                data_lines = []
                async for line in response.aiter_lines():
                    if line.startswith("data:"):
                        data_lines.append(line[5:].strip())
                    elif not line and data_lines:
                        raw = "\n".join(data_lines)
                        data_lines = []
                        if raw == "[DONE]":
                            return
                        chunk = json.loads(raw)
                        for choice in chunk.get("choices", []):
                            delta = choice.get("delta") or {}
                            if delta.get("content"):
                                yield {"type": "text", "text": delta["content"]}
                            for call in delta.get("tool_calls") or []:
                                yield {"type": "tool", "call": call}
                if data_lines:
                    raw = "\n".join(data_lines)
                    if raw != "[DONE]":
                        chunk = json.loads(raw)
                        for choice in chunk.get("choices", []):
                            delta = choice.get("delta") or {}
                            if delta.get("content"):
                                yield {"type": "text", "text": delta["content"]}
                            for call in delta.get("tool_calls") or []:
                                yield {"type": "tool", "call": call}
