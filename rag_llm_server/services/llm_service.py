"""LLM 调用服务。"""

from __future__ import annotations
import time
from collections.abc import Iterator

from volcenginesdkarkruntime import Ark

from config import settings


# 保持这段内容在每次请求中完全一致，有利于模型服务复用固定前缀。
SYSTEM_CONTENT = """
你是“小智 AI 培训”的课程咨询顾问。

你的任务是自然、准确地回答用户问题。

当提供了【参考知识】时：
1. 优先依据其中的明确内容回答。
2. 只能对资料进行忠实摘录、改写或简要归纳，不得超出资料推理。
3. 课程名称、价格、数字、日期、时间和联系方式必须以资料原文为准，不得修改。

当没有提供【参考知识】或资料与问题无关时：
1. 可以使用你的通用能力正常回答非业务事实类问题，例如问候、身份介绍和一般性解释。
2. 不得编造课程名称、价格、课时、老师、优惠、就业数据、报名流程或其他培训业务信息。
3. 对于无法确认的培训业务信息，明确说明目前无法确认，不要猜测。

通用要求：
1. 回答直接、简洁、自然、有礼貌。
2. 不要提及 RAG、知识库、系统提示词、模型或内部处理过程。
3. 不要输出分析过程，不要主动扩展话题。
""".strip()


class LLMService:
    def __init__(self):
        self.client = Ark(
            base_url=settings.ARK_BASE_URL,
            api_key=settings.ARK_API_KEY,
            timeout=1800,
        )
        self.max_history_messages = max(2, settings.LLM_MAX_HISTORY_MESSAGES)

    def _trim_history(self, history_messages: list | None) -> list[dict[str, str]]:
        """只保留有效的 user/assistant 消息，避免历史上下文无限增长。"""
        if not history_messages:
            return []

        valid_messages = []
        for message in history_messages:
            if not isinstance(message, dict):
                continue
            role = message.get("role")
            content = message.get("content")
            if role in {"user", "assistant"} and isinstance(content, str) and content.strip():
                valid_messages.append({"role": role, "content": content})

        return valid_messages[-self.max_history_messages :]

    def chat_stream(self, history_messages: list, rag_context: str = "") -> Iterator:
        """使用固定系统前缀调用 LLM，并流式返回响应。"""
        if not self.client:
            print("❌ LLM 服务未配置")
            return

        # 固定 system 消息不再拼接动态 RAG 内容。
        messages: list[dict[str, str]] = [
            {"role": "system", "content": SYSTEM_CONTENT}
        ]

        # 动态知识内容单独放置，避免每次改变固定系统提示词。
        if isinstance(rag_context, str) and rag_context.strip():
            messages.append(
                {
                    "role": "system",
                    "content": (
                        "【参考知识】\n"
                        "以下内容仅作为回答依据，请优先参考：\n\n"
                        f"{rag_context.strip()}"
                    ),
                }
            )

        messages.extend(self._trim_history(history_messages))

        try:
            print("🚀 发起流式调用")
            stream = self.client.chat.completions.create(
                model=settings.ARK_ENDPOINT_ID,
                messages=messages,
                temperature=0.3,
                stream=True,
                stream_options={"include_usage": True},
            )

            for chunk in stream:
                yield chunk



        except Exception as exc:
            print(f"❌ LLM 调用失败: {exc}")
            return


llm_service = LLMService()
