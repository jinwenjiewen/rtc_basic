"""火山引擎知识库检索服务。"""

from __future__ import annotations

import logging
import json
from typing import Any

import httpx

from config import settings


logger = logging.getLogger(__name__)


class RAGService:
    """在调用 LLM 前，从指定知识库检索与问题相关的文本。"""

    SEARCH_PATH = "/api/knowledge/collection/search_knowledge"
    _TEXT_KEYS = (
        "content",
        "text",
        "chunk_content",
        "chunk_text",
        "raw_text",
        "paragraph",
        "summary",
    )
    _RESULT_KEYS = (
        "result_list",
        "results",
        "items",
        "documents",
        "document",
        "chunks",
        "chunk",
        "chunk_info",
        "records",
        "data",
        "result",
    )

    def __init__(self) -> None:
        self.api_key = settings.KNOWLEDGE_BASE_API_KEY
        self.project = settings.KNOWLEDGE_BASE_PROJECT
        self.collection = settings.KNOWLEDGE_BASE_COLLECTION
        self.limit = max(1, settings.KNOWLEDGE_BASE_LIMIT)
        self.timeout = settings.KNOWLEDGE_BASE_TIMEOUT
        self.base_url = self._normalise_base_url(settings.KNOWLEDGE_BASE_DOMAIN)

    @staticmethod
    def _normalise_base_url(domain: str) -> str:
        """支持配置完整 URL，也支持文档中的纯域名写法。"""
        domain = (domain or "").strip().rstrip("/")
        if not domain:
            return ""
        if domain.startswith(("http://", "https://")):
            return domain
        # 与知识库 API 示例一致；如需 HTTPS，可在环境变量中配置完整 https URL。
        return f"http://{domain}"

    def _build_payload(self, query: str) -> dict[str, Any]:
        return {
            "project": self.project,
            "name": self.collection,
            "query": query,
            "limit": self.limit,
            "pre_processing": {
                "need_instruction": True,
                "return_token_usage": True,
                "messages": [
                    {"role": "system", "content": ""},
                    {"role": "user"},
                ],
            },
            "dense_weight": 0.5,
            "post_processing": {
                "get_attachment_link": True,
                "rerank_only_chunk": False,
                "rerank_switch": True,
            },
        }

    async def retrieve(self, query: str) -> str:
        """检索知识库并返回可直接作为 LLM 上下文的文本。

        所有异常都会降级为空字符串：RAG 是增强能力，不应让 RTC 流式对话失败。
        """
        question = query.strip() if isinstance(query, str) else ""
        if not question:
            return ""

        if not self.api_key:
            logger.warning("知识库未配置 KNOWLEDGE_BASE_API_KEY，已跳过检索")
            return ""
        if not self.base_url or not self.collection:
            logger.error("知识库域名或集合名称未配置，已跳过检索")
            return ""

        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json; charset=utf-8",
            "Authorization": f"Bearer {self.api_key}",
        }
        url = f"{self.base_url}{self.SEARCH_PATH}"

        try:
            print("\n========== RAG 知识库检索开始 ==========")
            print(f"请求地址: {url}")
            print(f"用户问题: {question}")
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(url, headers=headers, json=self._build_payload(question))
                # 先打印响应信息，再检查 HTTP 状态；这样即使请求失败也能定位原因。
                print(f"HTTP 状态码: {response.status_code}")
                print(f"响应对象: {response!r}")
                print("响应原始内容:")
                print(response.text)
                response.raise_for_status()
                payload = response.json()
                print("响应 JSON 内容:")
                print(json.dumps(payload, ensure_ascii=False, indent=2))
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("知识库检索失败：%s", exc)
            print("========== RAG 知识库检索结束（失败）==========\n")
            return ""

        if not isinstance(payload, dict):
            logger.warning("知识库返回格式错误：期望 JSON 对象")
            return ""

        # 火山引擎 API 的业务错误通常仍会返回 HTTP 200。
        code = payload.get("code")
        if code not in (None, 0, "0"):
            logger.warning("知识库返回业务错误，code=%s, message=%s", code, payload.get("message"))
            return ""

        context = self._extract_context(payload)
        logger.info("知识库检索完成：%d 个字符", len(context))
        print("提取后传给 LLM 的知识库内容:")
        print(context or "（未提取到可用正文）")
        print("========== RAG 知识库检索结束 ==========" + "\n")
        return context

    @classmethod
    def _extract_context(cls, payload: dict[str, Any]) -> str:
        """兼容知识库 API 不同版本的结果字段，提取并去重正文。"""
        texts: list[str] = []
        seen: set[str] = set()

        def append(value: Any) -> None:
            if isinstance(value, str):
                text = value.strip()
                if text and text not in seen:
                    seen.add(text)
                    texts.append(text)

        def walk(value: Any) -> None:
            if isinstance(value, list):
                for item in value:
                    walk(item)
                return
            if not isinstance(value, dict):
                return

            # 优先读取命中项的正文，避免把标题、文件名、URL 等元数据拼入上下文。
            for key in cls._TEXT_KEYS:
                append(value.get(key))
            for key in cls._RESULT_KEYS:
                child = value.get(key)
                if isinstance(child, (dict, list)):
                    walk(child)

        walk(payload.get("data", payload))
        return "\n\n".join(texts)


rag_service = RAGService()
