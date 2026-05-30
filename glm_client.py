"""GLM API 客户端封装，支持流式和非流式对话。"""

import json
import os
import sys
from typing import Generator, Optional

import requests
from dotenv import load_dotenv

load_dotenv()


class GLMClient:
    """智谱 GLM API 客户端。"""

    def __init__(
        self,
        api_key: Optional[str] = None,
        api_base: Optional[str] = None,
        model: str = "glm-5.1",
    ):
        self.api_key = api_key or os.getenv("GLM_API_KEY", "")
        self.api_base = (
            api_base
            or os.getenv("GLM_API_BASE")
            or "https://open.bigmodel.cn/api/coding/paas/v4"
        )
        self.model = model
        self.endpoint = f"{self.api_base}/chat/completions"

        if not self.api_key:
            raise ValueError(
                "未提供 GLM_API_KEY。请设置环境变量或在初始化时传入 api_key。"
            )

    def _build_headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    def _build_payload(
        self,
        messages: list[dict],
        temperature: float = 0.7,
        max_tokens: int = 16384,
        stream: bool = False,
    ) -> dict:
        return {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": stream,
        }

    def chat(
        self,
        user_message: str,
        system_message: Optional[str] = None,
        temperature: float = 0.7,
        max_tokens: int = 16384,
    ) -> str:
        """非流式对话，返回完整回复文本。

        支持 GLM 思考/推理模型：当 reasoning_content 存在但 content 为空时，
        会根据 finish_reason 决定是抛出 token 耗尽错误，还是返回推理内容。
        """
        messages = []
        if system_message:
            messages.append({"role": "system", "content": system_message})
        messages.append({"role": "user", "content": user_message})

        payload = self._build_payload(messages, temperature, max_tokens, stream=False)
        resp = requests.post(
            self.endpoint, headers=self._build_headers(), json=payload, timeout=600
        )
        resp.raise_for_status()
        data = resp.json()

        message = data["choices"][0]["message"]
        content = message.get("content", "")
        reasoning = message.get("reasoning_content", "")
        finish_reason = data["choices"][0].get("finish_reason", "")

        if not content and finish_reason == "length":
            raise RuntimeError(
                f"GLM 推理 token 耗尽（finish_reason=length），请增加 max_tokens。"
                f"reasoning 阶段已使用 "
                f"{data.get('usage', {}).get('completion_tokens', '?')} tokens。"
            )

        return content or reasoning

    def chat_stream(
        self,
        user_message: str,
        system_message: Optional[str] = None,
        temperature: float = 0.7,
        max_tokens: int = 16384,
    ) -> Generator[str, None, None]:
        """流式对话，逐 token 生成回复片段。

        支持 GLM 思考/推理模型：优先输出 content，若 content 为空则回退到
        reasoning_content。
        """
        messages = []
        if system_message:
            messages.append({"role": "system", "content": system_message})
        messages.append({"role": "user", "content": user_message})

        payload = self._build_payload(messages, temperature, max_tokens, stream=True)
        with requests.post(
            self.endpoint,
            headers=self._build_headers(),
            json=payload,
            stream=True,
            timeout=180,
        ) as resp:
            resp.raise_for_status()
            for line in resp.iter_lines(decode_unicode=True):
                if not line:
                    continue
                if line.startswith("data:"):
                    data_str = line[len("data:") :].strip()
                    if data_str == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data_str)
                        choice = chunk["choices"][0]
                        delta = choice["delta"]
                        # 优先输出 content，回退到 reasoning_content
                        if "content" in delta and delta["content"]:
                            yield delta["content"]
                        elif "reasoning_content" in delta and delta["reasoning_content"]:
                            yield delta["reasoning_content"]
                        # 检查流式结束时的 finish_reason
                        if choice.get("finish_reason") == "length":
                            raise RuntimeError(
                                "GLM 推理 token 耗尽（finish_reason=length），"
                                "请增加 max_tokens。"
                            )
                    except (json.JSONDecodeError, KeyError, IndexError):
                        continue


if __name__ == "__main__":
    client = GLMClient()

    # ---- 非流式示例 ----
    print("=== 非流式对话 ===")
    reply = client.chat(
        user_message="用一句话介绍你自己。",
        system_message="你是一个简洁的助手。",
    )
    print(reply)
    print()

    # ---- 流式示例 ----
    print("=== 流式对话 ===")
    for token in client.chat_stream(
        user_message="请写一首关于交通信号的四行小诗。",
        system_message="你是一位诗人。",
    ):
        print(token, end="", flush=True)
    print()
