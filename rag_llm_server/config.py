import os
from dotenv import load_dotenv

load_dotenv()

class Config:
    VOLC_AK = os.getenv("VOLC_ACCESS_KEY")
    VOLC_SK = os.getenv("VOLC_SECRET_KEY")
    ARK_ENDPOINT_ID = os.getenv("ARK_ENDPOINT_ID")
    ARK_API_KEY = os.getenv("ARK_API_KEY")
    ARK_BASE_URL=os.getenv("ARK_BASE_URL")
    LLM_MAX_HISTORY_MESSAGES = int(os.getenv("LLM_MAX_HISTORY_MESSAGES", "12"))
    Token = os.getenv("RTC_APP_KEY")
    RTC_APP_ID = os.getenv("RTC_APP_ID")
    RTC_APP_KEY = os.getenv("RTC_APP_KEY")
    
    SERVER_URL = os.getenv("SERVER_URL")

    # 知识库（火山引擎 Knowledge Base）配置。
    # 未配置 API Key 时，RAG 服务会跳过检索，不会影响 LLM 的正常回答。
    KNOWLEDGE_BASE_API_KEY = os.getenv("KNOWLEDGE_BASE_API_KEY")
    KNOWLEDGE_BASE_DOMAIN = os.getenv(
        "KNOWLEDGE_BASE_DOMAIN", "api-knowledgebase.mlp.cn-beijing.volces.com"
    )
    KNOWLEDGE_BASE_PROJECT = os.getenv("KNOWLEDGE_BASE_PROJECT", "default")
    KNOWLEDGE_BASE_COLLECTION = os.getenv("KNOWLEDGE_BASE_COLLECTION", "rtc_ai")
    KNOWLEDGE_BASE_LIMIT = int(os.getenv("KNOWLEDGE_BASE_LIMIT", "1"))
    KNOWLEDGE_BASE_TIMEOUT = float(os.getenv("KNOWLEDGE_BASE_TIMEOUT", "10"))

settings = Config()
