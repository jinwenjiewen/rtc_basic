import asyncio
import uuid
import time
import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Dict, Any

from config import settings
from services.llm_service import llm_service
from services.token_build import AccessToken, PRIVILEGES
from services.utils import Signer  # 确保 utils.py 已移动到 services 目录

from fastapi.responses import JSONResponse

from fastapi import Request
from fastapi.responses import StreamingResponse  # <--- 必须导入这个
import json
from starlette.concurrency import iterate_in_threadpool
from services.rag_service import rag_service  # <--- 新增这行

# 在你的 settings.py 或 main.py 顶部
from dotenv import load_dotenv

load_dotenv()  # 必须先执行这一行，后面的 settings 才能读到值

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# --- 1. 获取场景 (前端展示用) ---
@app.post("/getScenes")
async def get_scenes(request: Request):
    # 生成随机 ID
    room_id = "ChatRoom01"
    user_id = "Huoshan01"

    # 签发 RTC Token
    token_builder = AccessToken(
        settings.RTC_APP_ID, settings.RTC_APP_KEY, room_id, user_id
    )
    token_builder.add_privilege(PRIVILEGES["PrivSubscribeStream"], 0)
    token_builder.add_privilege(PRIVILEGES["PrivPublishStream"], 0)
    token_builder.expire_time(int(time.time()) + 3600 * 24)
    token = token_builder.serialize()

    # 构造返回结构
    return {
        "ResponseMetadata": {"Action": "getScenes"},
        "Result": {
            "scenes": [
                {
                    "scene": {
                        # --- 补全的核心字段 ---
                        "id": "Custom",  # 建议改为 Custom，通常前端会根据这个 ID 做特殊处理
                        "name": "自定义助手",
                        "botName": "ChatBot01",
                        "icon": "https://lf3-rtc-demo.volccdn.com/obj/rtc-aigc-assets/DoubaoAvatar.png",  # 补全图标
                        # --- 功能开关 ---
                        "isInterruptMode": True,  # 是否支持打断
                        "isVision": False,  # 补全：是否开启视觉（摄像头）
                        "isScreenMode": False,  # 补全：是否开启屏幕共享
                        # --- 数字人相关 (无数字人时设为 None/null) ---
                        "isAvatarScene": None,
                        "avatarBgUrl": None,
                    },
                    "rtc": {
                        "AppId": settings.RTC_APP_ID,
                        "RoomId": room_id,
                        "UserId": user_id,
                        "Token": settings.RTC_APP_KEY,
                    },
                    # 这里的配置主要是为了兼容前端透传，实际生效主要看 proxy
                    "VoiceChat": {},
                }
            ]
        },
    }


# --- 2. 拦截前端的 StartVoiceChat 请求 (核心配置下发) ---
# main.py 核心修改
# rag_llm_server/main.py


@app.post("/proxy")
async def proxy(request: Request):
    """
    完全硬编码的代理接口，用于测试链路是否畅通
    """
    action = request.query_params.get("Action")
    version = request.query_params.get("Version", "2024-12-01")

    # 打印前端实际传过来的数据，方便观察
    try:
        incoming_body = await request.json()
        print(f"DEBUG: 收到前端请求 {action}, Body: {incoming_body}")
    except:
        pass

    # --- 开始硬编码数据 ---
    # 注意：这里的 AppId, RoomId, UserId, Token 必须与你提供的 JSON 完全一致
    target_app_id = settings.RTC_APP_ID
    target_room_id = "ChatRoom01"
    target_user_id = "Huoshan01"

    request_body = {}

    print(f"RTCCCCC  callback {settings.SERVER_URL}/api/chat_callback")
    if action == "StartVoiceChat":
        request_body = {
            "AppId": target_app_id,
            "RoomId": target_room_id,
            "TaskId": "ChatTask01",
            "AgentConfig": {
                "TargetUserId": [target_user_id],
                "WelcomeMessage": "我是小智，你的专属课程顾问，有什么问题尽管问我吧！",
                "UserId": "ChatBot01",
                "EnableConversationStateCallback": True, 
            },
            "Config": {
                "ASRConfig": {
                    "Provider": "volcano",
                    "ProviderParams": {
                        "Mode": "smallmodel",
                        "AppId": "1168243193",
                        "Cluster": "volcengine_streaming_common",
                    },
                },
                "TTSConfig": {
                    "Provider": "volcano",
                    "ProviderParams": {
                        "app": {"appid": "1168243193", "cluster": "volcano_tts"},
                        "audio": {
                            "voice_type": "BV001_streaming",
                            "speed_ratio": 1,
                            "pitch_ratio": 1,
                            "volume_ratio": 1,
                        },
                    },
                },
                "LLMConfig": {
                    # 先用 Custom 模式测试你的回调地址
                    "Mode": "CustomLLM",
                    "Url": f"{settings.SERVER_URL}/api/chat_callback",
                    # "Mode": "ArkV3",
                    # "EndPointId": "ep-20260821161719-qj5nr",
                    "Method": "POST",
                    "ApiType": "https"
                    if str(settings.SERVER_URL).startswith("https")
                    else "http",
                },
                "InterruptMode": 0,
            },
        }
    elif action == "StopVoiceChat":
        request_body = {
            "AppId": target_app_id,
            "RoomId": target_room_id,
            "TaskId": "ChatTask01",
        }
    else:
        # 其他 Action 直接返回前端传的内容
        request_body = incoming_body

    # --- 签名与发送 ---
    host = "rtc.volcengineapi.com"
    open_api_request_data = {
        "method": "POST",
        "path": "/",
        "params": {"Action": action, "Version": version},
        "headers": {"Host": host, "Content-Type": "application/json"},
        "body": request_body,
    }

    # 这里的 AK/SK 必须拥有调用 RTC OpenAPI 的权限
    account_config = {"accessKeyId": settings.VOLC_AK, "secretKey": settings.VOLC_SK}

    signer = Signer(open_api_request_data, "rtc")
    signer.add_authorization(account_config)

    url = f"https://{host}?Action={action}&Version={version}"

    # print(f"DEBUG: 发送请求到 {url} callback rtc")
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            url,
            headers=open_api_request_data["headers"],
            json=request_body,
            timeout=30.0,
        )
        result = resp.json()
        print(f"DEBUG: 火山引擎返回结果: {result}")
        return result


# --- 3. 业务回调接口 (RTC -> 这里) ---


# ... 其他代码 ...


@app.post("/api/chat_callback")
async def chat_callback(request: Request):
    """RTC CustomLLM 的 OpenAI 兼容 SSE 回调接口。"""
    try:
        data = await request.json()
    except Exception as exc:
        print(f"⚠️ 无法解析 RTC LLM 请求: {exc}")
        data = {}

    print("======================== RTC 流式请求")
    print(json.dumps(data, ensure_ascii=False))

    messages = data.get("messages")

    async def generate_sse():
        # 无效请求也保持 SSE 协议，避免返回普通 JSON 破坏 RTC 客户端解析。
        if not isinstance(messages, list) or not messages:
            print("⚠️ 忽略：messages 为空或格式不正确")
            yield "data: [DONE]\n\n"
            return

        last_message = messages[-1]
        if not isinstance(last_message, dict) or last_message.get("role") != "user":
            print("⚠️ 忽略：最后一条消息不是用户消息")
            yield "data: [DONE]\n\n"
            return

        try:
            question = last_message.get("content", "")
            rag_content = await rag_service.retrieve(question)
            stream_iterator = llm_service.chat_stream(messages, rag_content)

            # Ark SDK 的流式迭代器是同步的，在线程池中拉取下一个 chunk，
            # 避免网络等待阻塞 FastAPI 的事件循环。
            async for chunk in iterate_in_threadpool(stream_iterator):
                if await request.is_disconnected():
                    print("⚠️ RTC SSE 客户端已断开")
                    return

                if chunk is None:
                    continue

                if hasattr(chunk, "model_dump_json"):
                    chunk_json = chunk.model_dump_json()
                elif isinstance(chunk, dict):
                    chunk_json = json.dumps(
                        chunk,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                else:
                    print(f"⚠️ 忽略无法序列化的 LLM chunk: {type(chunk).__name__}")
                    continue

                yield f"data: {chunk_json}\n\n"

        except asyncio.CancelledError:
            print("⚠️ RTC SSE 请求被取消")
            raise
        except Exception as exc:
            # 响应头已是 SSE；此处仅记录错误，仍按协议发送结束符。
            print(f"❌ RTC SSE 处理失败: {type(exc).__name__}: {exc}")

        yield "data: [DONE]\n\n"

    return StreamingResponse(
        generate_sse(),
        status_code=200,
        media_type="text/event-stream",  # <--- 必须是这个 Header
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
            "Access-Control-Allow-Origin": "*",
        },
    )


from typing import List, Optional


# 1. 定义消息模型
class ChatMessage(BaseModel):
    role: str  # "user" 或 "assistant"
    content: str


class DebugRequest(BaseModel):
    history: Optional[List[ChatMessage]] = None
    question: str


# 2. 调试接口
@app.post("/debug/chat")
async def debug_chat(request: DebugRequest):



    history = request.history or []
    current_messages = [
        {"role": msg.role, "content": msg.content}
        for msg in history
    ]
    current_messages.append(
        {"role": "user", "content": request.question}
    )

    async def collect_answer():

        # 1、记录总时间开始
        start_t = time.time()

        rag_content = await rag_service.retrieve(request.question)

        rag_duration = time.time() - start_t
        print(f"知识库查询耗时：{rag_duration}s")


        llm_start_t = time.time()


        stream_iterator = llm_service.chat_stream(current_messages, rag_content)

        parts = []
        usage = None

        try:
            async for chunk in iterate_in_threadpool(stream_iterator):
                if chunk is None:
                    continue

                if isinstance(chunk, dict):
                    choices = chunk.get("choices") or []
                    if choices:
                        delta = choices[0].get("delta") or {}
                        content = delta.get("content")
                        if content:
                            parts.append(content)
                    if chunk.get("usage"):
                        usage = chunk["usage"]
                    continue

                choices = getattr(chunk, "choices", None) or []
                if choices:
                    delta = getattr(choices[0], "delta", None)
                    content = getattr(delta, "content", None)
                    if content:
                        parts.append(content)
                chunk_usage = getattr(chunk, "usage", None)
                if chunk_usage:
                    usage = chunk_usage.model_dump() if hasattr(chunk_usage, "model_dump") else chunk_usage

            llm_duration = time.time() - llm_start_t

            print(f"大模型查询耗时：{llm_duration}s")

        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"❌ /debug/chat 失败: {exc}")

        return {"text": "".join(parts), "usage": usage}



    return await collect_answer()


# ... 其他导入保持不变 ...
from services.rag_service import rag_service  # 确保已导入 rag_service


# --- 新增：知识库调试接口 ---
@app.get("/debug/rag")
async def debug_rag(query: str):
    """
    调试接口：直接返回知识库检索到的原始文本内容
    用法：浏览器访问 http://127.0.0.1:8000/debug/rag?query=你的问题
    """
    if not query:
        return {"error": "请提供 query 参数"}

    print(f"🔍 [Debug] 正在检索知识库: {query}")

    # 调用我们在 rag_service.py 中实现的异步 retrieve 方法
    context = await rag_service.retrieve(query)

    return {
        "query": query,
        "retrieved_context": context,
        "length": len(context) if context else 0,
        "status": "success" if context else "no_results_or_error",
    }






if __name__ == "__main__":
    import uvicorn

    print(f"🚀 Server running at {settings.SERVER_URL}")
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=3001,
        reload=True,
        reload_dirs=[".", "services"],
        # 依然建议排除缓存文件，防止编译行为触发重启
        reload_excludes=[
            "*/__pycache__/*",
            "*.pyc",
            ".venv/*",  # 排除根目录下的虚拟环境
            "*/.venv/*",
        ],
    )
