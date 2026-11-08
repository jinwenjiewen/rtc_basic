"""PostgreSQL connection and versioned schema initialization."""
from functools import lru_cache
from pathlib import Path

from alembic import command
from alembic.config import Config as AlembicConfig
from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session

from config import settings


class Base(DeclarativeBase):
    pass


@lru_cache
def get_engine():
    if not settings.DATABASE_URL.startswith("postgresql+psycopg://"):
        raise RuntimeError("请在 rag_llm_server/.env 配置 PostgreSQL DATABASE_URL（postgresql+psycopg://...）。")
    return create_engine(settings.DATABASE_URL, pool_pre_ping=True, connect_args={"connect_timeout": 5})


def get_db():
    with Session(get_engine(), expire_on_commit=False) as db:
        yield db


def initialize_database():
    from auth import create_user
    from models import User
    from sqlalchemy import select, text

    if len(settings.JWT_SECRET_KEY.encode()) < 32:
        raise RuntimeError("JWT_SECRET_KEY 至少需要 32 字节的随机密钥。")
    if len(settings.RTC_CALLBACK_SECRET) < 32:
        raise RuntimeError("请配置至少 32 字符的随机 RTC_CALLBACK_SECRET。")
    if not settings.AUTH_ALLOWED_ORIGINS or "*" in settings.AUTH_ALLOWED_ORIGINS:
        raise RuntimeError("AUTH_ALLOWED_ORIGINS 必须是明确的前端地址，不能为 *。")
    if settings.ACCESS_TOKEN_MINUTES <= 0 or settings.REFRESH_TOKEN_DAYS <= 0:
        raise RuntimeError("登录有效期必须大于 0。")

    migration_config = AlembicConfig(str(Path(__file__).with_name("alembic.ini")))
    # Serialize migrations/bootstrap across concurrent uvicorn workers.
    with get_engine().connect() as connection:
        connection.execute(text("SELECT pg_advisory_lock(73481529)"))
        connection.commit()
        try:
            migration_config.attributes["connection"] = connection
            command.upgrade(migration_config, "head")
            with Session(bind=connection) as db:
                if not db.scalar(select(User.id).limit(1)):
                    if not settings.ADMIN_PASSWORD:
                        raise RuntimeError("首次启动需要在 .env 设置 ADMIN_PASSWORD，或使用 manage.py create-user 创建账号。")
                    create_user(db, settings.ADMIN_USERNAME, settings.ADMIN_PASSWORD)
        finally:
            connection.execute(text("SELECT pg_advisory_unlock(73481529)"))
            connection.commit()
