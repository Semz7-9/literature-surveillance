"""
LLM API 客户端

提供统一接口，支持多个 provider
"""

import json
from typing import Type, TypeVar, Any
from pydantic import BaseModel
import httpx

T = TypeVar("T", bound=BaseModel)


class LLMClient:
    """LLM API 统一客户端"""

    def __init__(
        self,
        provider: str,
        api_key: str,
        model: str,
        timeout: float = 60.0,
    ):
        """
        Args:
            provider: "openai" 或 "anthropic"
            api_key: API key
            model: 模型名称
            timeout: 请求超时（秒）
        """
        self.provider = provider.lower()
        self.api_key = api_key
        self.model = model
        self.timeout = timeout

        # 设置 base URL
        if self.provider == "openai":
            self.base_url = "https://api.openai.com/v1"
        elif self.provider == "anthropic":
            self.base_url = "https://api.anthropic.com/v1"
        elif self.provider == "deepseek":
            self.base_url = "https://api.deepseek.com"
        else:
            raise ValueError(f"Unsupported provider: {provider}")

        self._client = httpx.AsyncClient(timeout=timeout)

    async def call_with_schema(
        self,
        prompt: str,
        schema: Type[T],
        system_prompt: str | None = None,
    ) -> T:
        """
        调用 LLM 并强制返回符合 schema 的结构化输出

        Args:
            prompt: 用户 prompt
            schema: Pydantic model class
            system_prompt: 系统 prompt（可选）

        Returns:
            符合 schema 的结构化输出

        Raises:
            httpx.HTTPStatusError: API 错误
            ValueError: 输出不符合 schema
        """
        if self.provider in ("openai", "deepseek"):
            return await self._call_openai(prompt, schema, system_prompt)
        elif self.provider == "anthropic":
            return await self._call_anthropic(prompt, schema, system_prompt)
        else:
            raise ValueError(f"Unsupported provider: {self.provider}")

    async def _call_openai(
        self,
        prompt: str,
        schema: Type[T],
        system_prompt: str | None,
    ) -> T:
        """OpenAI API 调用"""
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})

        # 使用 response_format 强制 JSON 输出
        payload = {
            "model": self.model,
            "messages": messages,
            "response_format": {"type": "json_object"},
            "temperature": 0.3,
        }

        # 将 schema 添加到 prompt 中
        schema_json = schema.model_json_schema()
        enhanced_prompt = f"{prompt}\n\nOutput valid JSON matching this schema:\n{json.dumps(schema_json, indent=2)}"
        messages[-1]["content"] = enhanced_prompt

        response = await self._client.post(
            f"{self.base_url}/chat/completions",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
        )
        response.raise_for_status()

        data = response.json()
        content = data["choices"][0]["message"]["content"]

        # 解析为 schema
        try:
            parsed = json.loads(content)
            return schema(**parsed)
        except Exception as e:
            raise ValueError(f"Failed to parse LLM output: {e}\nContent: {content}")

    async def _call_anthropic(
        self,
        prompt: str,
        schema: Type[T],
        system_prompt: str | None,
    ) -> T:
        """Anthropic API 调用"""
        # Anthropic 不支持原生 JSON mode，需要在 prompt 中要求
        schema_json = schema.model_json_schema()
        enhanced_prompt = f"{prompt}\n\nOutput ONLY valid JSON matching this schema (no markdown, no explanation):\n{json.dumps(schema_json, indent=2)}"

        payload = {
            "model": self.model,
            "max_tokens": 4096,
            "messages": [{"role": "user", "content": enhanced_prompt}],
            "temperature": 0.3,
        }

        if system_prompt:
            payload["system"] = system_prompt

        response = await self._client.post(
            f"{self.base_url}/messages",
            headers={
                "x-api-key": self.api_key,
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json",
            },
            json=payload,
        )
        response.raise_for_status()

        data = response.json()
        content = data["content"][0]["text"]

        # 清理可能的 markdown 包装
        content = content.strip()
        if content.startswith("```json"):
            content = content[7:]
        if content.startswith("```"):
            content = content[3:]
        if content.endswith("```"):
            content = content[:-3]
        content = content.strip()

        # 解析为 schema
        try:
            parsed = json.loads(content)
            return schema(**parsed)
        except Exception as e:
            raise ValueError(f"Failed to parse LLM output: {e}\nContent: {content}")

    async def close(self):
        """关闭 HTTP client"""
        await self._client.aclose()

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()


async def create_llm_client(config: dict, model_tier: str = "cheap") -> LLMClient:
    """
    从配置创建 LLM 客户端

    Args:
        config: LLM 配置字典
        model_tier: "cheap", "balanced", 或 "strong"

    Returns:
        LLMClient 实例
    """
    model_key = f"model_{model_tier}"
    if model_key not in config:
        raise ValueError(f"Unknown model tier: {model_tier}")

    return LLMClient(
        provider=config["provider"],
        api_key=config["api_key"],
        model=config[model_key],
    )
