import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).with_name(".env"))

class Config:
    DATABASE_URL = os.getenv("DATABASE_URL", "")
    JWT_SECRET_KEY = os.getenv("JWT_SECRET_KEY", "")
    JWT_ISSUER = "rtc-basic"
    JWT_AUDIENCE = "rtc-basic-web"
    ACCESS_TOKEN_MINUTES = int(os.getenv("ACCESS_TOKEN_MINUTES", "15"))
    REFRESH_TOKEN_DAYS = int(os.getenv("REFRESH_TOKEN_DAYS", "7"))
    AUTH_COOKIE_SECURE = os.getenv("AUTH_COOKIE_SECURE", "false").lower() in {"1", "true", "yes"}
    AUTH_ALLOWED_ORIGINS = [origin.strip().rstrip("/") for origin in os.getenv(
        "AUTH_ALLOWED_ORIGINS", "http://localhost:3000,http://127.0.0.1:3000"
    ).split(",") if origin.strip()]
    ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
    ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")
    RTC_CALLBACK_SECRET = os.getenv("RTC_CALLBACK_SECRET", "")

    VOLC_AK = os.getenv("VOLC_ACCESS_KEY")
    VOLC_SK = os.getenv("VOLC_SECRET_KEY")
    ARK_ENDPOINT_ID = os.getenv("ARK_ENDPOINT_ID")
    ARK_API_KEY = os.getenv("ARK_API_KEY")
    ARK_BASE_URL=os.getenv("ARK_BASE_URL")
    LLM_MAX_HISTORY_MESSAGES = int(os.getenv("LLM_MAX_HISTORY_MESSAGES", "12"))

    # 意图识别模型可使用独立密钥和地址；空密钥默认复用主模型的 ARK_API_KEY。
    INTENTION_API_KEY = os.getenv("INTENTION_API_KEY") or ARK_API_KEY
    INTENTION_BASE_URL = (
        os.getenv("INTENTION_BASE_URL") or "https://ark.cn-beijing.volces.com/api/v3"
    )
    INTENTION_MODEL = os.getenv("INTENTION_MODEL") or "doubao-seed-2-0-mini-260428"
    INTENTION_TIMEOUT = float(os.getenv("INTENTION_TIMEOUT", "5"))
    INTENTION_ENABLED = os.getenv("INTENTION_ENABLED", "true").lower() in {"1", "true", "yes"}
    RTC_APP_ID = os.getenv("RTC_APP_ID")
    RTC_APP_KEY = os.getenv("RTC_APP_KEY", "").strip()
    RTC_STATIC_TOKEN = os.getenv("RTC_STATIC_TOKEN", "").strip()
    RTC_ROOM_ID = os.getenv("RTC_ROOM_ID", "").strip()
    RTC_USER_ID = os.getenv("RTC_USER_ID", "").strip()
    RTC_TASK_ID = os.getenv("RTC_TASK_ID", "ChatTask01").strip()
    
    SERVER_URL = os.getenv("SERVER_URL", "").strip().rstrip("/")

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
