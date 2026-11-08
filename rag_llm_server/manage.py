"""Local administration; passwords are prompted rather than passed on the command line."""
import argparse
import getpass

import psycopg
from psycopg import sql
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session
from sqlalchemy import select, update

from auth import create_user, password_hasher, utcnow
from config import settings
from database import get_engine, initialize_database
from models import LoginSession, User


def init_db():
    url = make_url(settings.DATABASE_URL)
    if url.drivername != "postgresql+psycopg" or not url.database:
        raise ValueError("请配置有效的 PostgreSQL DATABASE_URL。")
    with psycopg.connect(host=url.host, port=url.port or 5432, user=url.username,
                        password=url.password, dbname="postgres", autocommit=True) as connection:
        exists = connection.execute("SELECT 1 FROM pg_database WHERE datname = %s", (url.database,)).fetchone()
        if not exists:
            connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(url.database)))
    initialize_database()
    print("数据库与账号表已就绪。首次账号来自 .env 的 ADMIN_USERNAME / ADMIN_PASSWORD。")


def main():
    parser = argparse.ArgumentParser(description="RTC 登录账号管理")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init-db", help="创建数据库、执行迁移并初始化首次账号")
    user_parser = commands.add_parser("create-user", help="创建登录账号")
    user_parser.add_argument("username")
    password_parser = commands.add_parser("set-password", help="重置账号密码并撤销该账号的所有登录")
    password_parser.add_argument("username")
    args = parser.parse_args()
    try:
        if args.command == "init-db":
            init_db()
        else:
            password = getpass.getpass("密码（12–128 位）：")
            if password != getpass.getpass("再次输入密码："):
                raise ValueError("两次输入的密码不一致。")
            if not 12 <= len(password) <= 128:
                raise ValueError("密码需为 12–128 位字符。")
            with Session(get_engine()) as db:
                if args.command == "create-user":
                    create_user(db, args.username, password)
                    print("账号已创建。")
                else:
                    user = db.scalar(select(User).where(User.username == args.username.strip().lower()))
                    if not user:
                        raise ValueError("账号不存在。")
                    user.password_hash = password_hasher.hash(password)
                    db.execute(update(LoginSession).where(LoginSession.user_id == user.id).values(revoked_at=utcnow()))
                    db.commit()
                    print("密码已重置，该账号的所有登录已失效。")
    except ValueError as exc:
        parser.exit(1, f"{exc}\n")


if __name__ == "__main__":
    main()
