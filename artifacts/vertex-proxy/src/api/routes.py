"""FastAPI路由模块"""

import json
import time
import uuid
from typing import Any, cast
import collections.abc
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse, JSONResponse, RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp

from src.core import MODELS_CONFIG_FILE
from src.core.errors import (
    VertexError,
    InvalidArgumentError,
    InternalError,
    AuthenticationError,
    PermissionDeniedError,
    NotFoundError,
    RateLimitError,
)
from src.api.vertex_client import VertexAIClient
from src.api.openai_compat import (
    openai_request_to_gemini,
    gemini_response_to_openai,
    stream_gemini_as_openai,
    convert_realtime_chunk,
)
from src.core.auth import api_key_manager
from src.utils.logger import get_logger, set_request_id
from src.utils.error_logger import save_error_snapshot

logger = get_logger(__name__)


def _vertex_error_to_oai(e: VertexError) -> dict[str, Any]:
    """将内部 VertexError 转为 OpenAI 错误格式（按子类分类）"""
    if isinstance(e, (InvalidArgumentError, NotFoundError)):
        err_type = "invalid_request_error"
    elif isinstance(e, RateLimitError):
        err_type = "rate_limit_error"
    elif isinstance(e, (AuthenticationError, PermissionDeniedError)):
        err_type = "authentication_error"
    elif isinstance(e, InternalError):
        err_type = "server_error"
    else:
        err_type = "api_error"
    return {
        "error": {
            "message": e.message,
            "type": err_type,
            "code": e.status if hasattr(e, "status") and e.status else None,
        }
    }


def extract_api_key_from_request(request: Request) -> str | None:
    """
    从请求中提取API密钥，支持三种方式（按优先级）：
    1. Authorization: Bearer <key>  (OpenAI 标准)
    2. x-goog-api-key: <key>       (Gemini 标准 Header)
    3. ?key=<key>                   (Gemini 标准 Query Param)
    """
    # 1. OpenAI 风格: Authorization: Bearer <key>
    auth_header = request.headers.get("authorization")
    if auth_header and auth_header.lower().startswith("bearer "):
        token = auth_header[7:].strip()
        if token:
            return token

    # 2. Gemini 风格: x-goog-api-key
    goog_api_key = request.headers.get("x-goog-api-key")
    if goog_api_key:
        return goog_api_key.strip()

    # 3. URL Query Parameter: ?key=
    query_key = request.query_params.get("key")
    if query_key:
        return query_key.strip()

    return None


class APIKeyMiddleware(BaseHTTPMiddleware):
    """API密钥认证中间件"""

    def __init__(self, app: ASGIApp, excluded_paths: list[str] | None = None):
        super().__init__(app)
        self.excluded_paths: list[str] = excluded_paths or ["/", "/health"]

    async def dispatch(self, request: Request, call_next: collections.abc.Callable[[Request], collections.abc.Awaitable[Any]]):
        set_request_id()

        path = request.url.path
        method = request.method
        client_ip = request.client.host if request.client else "unknown"

        logger.debug(f"收到请求: {method} {path} from {client_ip}")

        if self.excluded_paths and any(
            path == p or path.startswith(p + "/") for p in self.excluded_paths
        ):
            logger.debug(f"路径 {path} 在排除列表中，跳过认证")
            return await call_next(request)

        api_key = extract_api_key_from_request(request)
        if not api_key:
            logger.warning(f"请求 {path} 缺少 API 密钥")
            return JSONResponse(
                status_code=401,
                content={
                    "error": {
                        "code": 401,
                        "message": "Method doesn't allow unregistered callers. Please use API Key or other form of API consumer identity to call this API.",
                        "status": "UNAUTHENTICATED"
                    }
                }
            )

        if not api_key_manager.validate_key(api_key):
            logger.warning(f"请求 {path} 使用了无效的 API 密钥: {api_key[:8]}...")
            return JSONResponse(
                status_code=400,
                content={
                    "error": {
                        "code": 400,
                        "message": "API key not valid. Please pass a valid API key.",
                        "status": "INVALID_ARGUMENT"
                    }
                }
            )

        request.state.api_key = api_key
        logger.debug(f"API 密钥验证成功: {api_key[:8]}...")

        start_time = time.time()
        response = await call_next(request)
        process_time = time.time() - start_time

        logger.info(f"{method} {path} - {response.status_code} ({process_time:.3f}s)")

        return response


def create_app(vertex_client: VertexAIClient) -> FastAPI:
    """创建FastAPI应用"""
    logger.info("创建 FastAPI 应用")

    app = FastAPI(
        title="Vertex AI Proxy (Anonymous)",
        description="Vertex AI 代理服务，兼容 Gemini API 和 OpenAI API",
        version="1.2.0"
    )

    app.add_middleware(
        APIKeyMiddleware,
        excluded_paths=["/", "/health", "/proxy-manager", "/admin", "/api/admin"],
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["*"],
    )

    # ==================== 全局异常处理 ====================

    @app.exception_handler(VertexError)
    async def vertex_exception_handler(request: Request, exc: VertexError):  # type: ignore[misc]
        logger.error(f"VertexError: {exc.message} (code={exc.code}, status={exc.status})")
        return JSONResponse(status_code=exc.code, content=exc.to_Dict())

    @app.exception_handler(Exception)
    async def general_exception_handler(request: Request, exc: Exception):  # type: ignore[misc]
        logger.error(f"Unhandled Exception: {exc}", exc_info=True)
        error = InternalError(message=str(exc))
        return JSONResponse(status_code=500, content=error.to_Dict())

    # ==================== 基础端点 ====================

    async def root():
        return RedirectResponse(url="/admin")
    app.get("/")(root)

    async def health_check() -> dict[str, str | int]:
        api_keys_count = len(api_key_manager.api_keys)
        return {
            "status": "healthy",
            "timestamp": int(time.time()),
            "api_keys_loaded": api_keys_count
        }
    app.get("/health")(health_check)

    # ==================== Gemini 原生端点 ====================

    async def list_models_gemini() -> dict[str, Any]:
        """Gemini 格式的模型列表"""
        current_time = int(time.time())
        models: list[str] = _load_models_config()
        return {
            "object": "list",
            "data": [
                {"id": m, "object": "model", "created": current_time, "owned_by": "google"}
                for m in models
            ]
        }
    app.get("/v1beta/models")(list_models_gemini)

    async def stream_generate_content(model: str, request: Request) -> StreamingResponse | JSONResponse:
        """Gemini 格式的流式生成接口"""
        logger.info(f"[Gemini] 流式请求: 模型={model}")
        try:
            body_any = await request.json()
        except json.JSONDecodeError as e:
            raise InvalidArgumentError(f"Invalid JSON in request body: {e}")

        if not isinstance(body_any, dict):
            raise InvalidArgumentError("Request body must be a JSON object")
        body: dict[str, Any] = cast(dict[str, Any], body_any)

        async def stream_generator():
            try:
                async for chunk in vertex_client.stream_chat(model=model, gemini_payload=body):
                    yield chunk
            except VertexError as e:
                try:
                    save_error_snapshot(
                        downstream_payload={"model": model, "stream": True, "body": body},
                        upstream_payload={"gemini_payload": body},
                        upstream_response=getattr(e, "upstream_response", "") or e.message,
                        error_type=f"gemini_stream_{type(e).__name__}",
                    )
                except Exception:
                    pass
                yield e.to_sse()
            except Exception as e:
                try:
                    save_error_snapshot(
                        downstream_payload={"model": model, "stream": True, "body": body},
                        upstream_payload={"gemini_payload": body},
                        upstream_response=str(e),
                        error_type=f"gemini_stream_unhandled_{type(e).__name__}",
                    )
                except Exception:
                    pass
                error = InternalError(message=str(e))
                yield error.to_sse()

        return StreamingResponse(stream_generator(), media_type="application/json")
    app.post("/v1beta/models/{model}:streamGenerateContent", response_model=None)(stream_generate_content)

    async def generate_content(model: str, request: Request) -> JSONResponse | dict[str, Any]:
        """Gemini 格式的非流式生成接口"""
        logger.info(f"[Gemini] 普通请求: 模型={model}")
        try:
            body_any = await request.json()
        except json.JSONDecodeError as e:
            raise InvalidArgumentError(f"Invalid JSON in request body: {e}")

        if not isinstance(body_any, dict):
            raise InvalidArgumentError("Request body must be a JSON object")
        body: dict[str, Any] = cast(dict[str, Any], body_any)

        start_time = time.time()
        try:
            response = await vertex_client.complete_chat(model=model, gemini_payload=body)
        except VertexError as e:
            try:
                save_error_snapshot(
                    downstream_payload={"model": model, "stream": False, "body": body},
                    upstream_payload={"gemini_payload": body},
                    upstream_response=getattr(e, "upstream_response", "") or e.message,
                    error_type=f"gemini_nonstream_{type(e).__name__}",
                )
            except Exception:
                pass
            raise
        process_time = time.time() - start_time
        logger.success(f"[Gemini] 完成: 模型={model}, 耗时={process_time:.3f}s")
        return response
    app.post("/v1beta/models/{model}:generateContent", response_model=None)(generate_content)

    # ==================== OpenAI 兼容端点 ====================

    async def list_models_openai() -> dict[str, Any]:
        """OpenAI 格式的模型列表 (/v1/models)"""
        current_time = int(time.time())
        models: list[str] = _load_models_config()
        entries = []
        for m in models:
            entries.append({
                "id": m,
                "object": "model",
                "created": current_time,
                "owned_by": "google",
                "permission": [],
                "root": m,
                "parent": None
            })
            entries.append({
                "id": f"fs-{m}",
                "object": "model",
                "created": current_time,
                "owned_by": "google",
                "permission": [],
                "root": m,
                "parent": None
            })
        return {"object": "list", "data": entries}
    app.get("/v1/models")(list_models_openai)

    async def openai_chat_completions(request: Request) -> StreamingResponse | JSONResponse:
        """
        OpenAI 兼容的 /v1/chat/completions 端点。
        自动将请求转换为 Gemini 格式，并将响应转换回 OpenAI 格式。
        """
        try:
            body_any = await request.json()
        except json.JSONDecodeError as e:
            raise InvalidArgumentError(f"Invalid JSON in request body: {e}")

        if not isinstance(body_any, dict):
            raise InvalidArgumentError("Request body must be a JSON object")
        body: dict[str, Any] = cast(dict[str, Any], body_any)

        is_stream = bool(body.get("stream", False))

        # 检测假流式前缀 fs-：先收完整响应，再拆成小块逐字发出
        raw_model = body.get("model", "")
        fake_stream = isinstance(raw_model, str) and raw_model.startswith("fs-")
        if fake_stream:
            body = {**body, "model": raw_model[3:]}  # 去掉 fs- 再传给 Gemini

        model, gemini_payload = openai_request_to_gemini(body)

        logger.info(f"[OpenAI] 请求: 模型={model}, 流式={is_stream}, 假流式={fake_stream}")

        if is_stream:
            completion_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
            created = int(time.time())

            async def openai_stream():
                try:
                    if fake_stream:
                        # 假流式：保留旧路径（聚合后小块逐字发出）
                        gemini_gen = vertex_client.stream_chat(model=model, gemini_payload=gemini_payload)
                        async for chunk in stream_gemini_as_openai(gemini_gen, model, fake_stream=True):
                            yield chunk
                    else:
                        # 真流式：逐 result 块转 OAI delta，渐进显示
                        is_first = True
                        has_finish = False
                        # finish_reason 推迟到所有内容发完之后再发，
                        # 避免上游把 finish 块放在内容前导致客户端提前截断
                        deferred_finish: str | None = None
                        async for gemini_chunk in vertex_client.stream_chat_realtime(
                            model=model, gemini_payload=gemini_payload
                        ):
                            events = convert_realtime_chunk(
                                gemini_chunk, model, completion_id, created, is_first=is_first
                            )
                            is_first = False
                            for ev in events:
                                ev_has_finish = '"finish_reason"' in ev and '"finish_reason": null' not in ev
                                if ev_has_finish:
                                    # 暂存最后一个 finish 事件，留到末尾发
                                    deferred_finish = ev
                                    continue
                                yield ev
                        if deferred_finish:
                            yield deferred_finish
                        else:
                            # 上游一次都没给 finish_reason → 兜底补一个
                            tail = {
                                "id": completion_id,
                                "object": "chat.completion.chunk",
                                "created": created,
                                "model": model,
                                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                            }
                            yield f"data: {json.dumps(tail, ensure_ascii=False)}\n\n"
                        yield "data: [DONE]\n\n"
                except VertexError as e:
                    try:
                        snapshot_path = save_error_snapshot(
                            downstream_payload={"model": model, "stream": True, "fake_stream": fake_stream, "body": body},
                            upstream_payload={"gemini_payload": gemini_payload},
                            upstream_response=getattr(e, "upstream_response", "") or e.message,
                            error_type=f"oai_stream_{type(e).__name__}",
                        )
                        if snapshot_path:
                            logger.warning(f"OAI 流式异常已保存快照: {snapshot_path}")
                    except Exception:
                        pass
                    err_resp = _vertex_error_to_oai(e)
                    yield f"data: {json.dumps(err_resp, ensure_ascii=False)}\n\n"
                    yield "data: [DONE]\n\n"
                except Exception as e:
                    try:
                        save_error_snapshot(
                            downstream_payload={"model": model, "stream": True, "fake_stream": fake_stream, "body": body},
                            upstream_payload={"gemini_payload": gemini_payload},
                            upstream_response=str(e),
                            error_type=f"oai_stream_unhandled_{type(e).__name__}",
                        )
                    except Exception:
                        pass
                    err_resp = {
                        "error": {
                            "message": str(e),
                            "type": "server_error",
                            "code": None,
                        }
                    }
                    yield f"data: {json.dumps(err_resp, ensure_ascii=False)}\n\n"
                    yield "data: [DONE]\n\n"

            return StreamingResponse(
                openai_stream(),
                media_type="text/event-stream; charset=utf-8",
                headers={
                    "Cache-Control": "no-cache",
                    "X-Accel-Buffering": "no",
                    "Content-Encoding": "identity",
                    "X-Content-Type-Options": "nosniff",
                }
            )
        else:
            start_time = time.time()
            try:
                gemini_resp = await vertex_client.complete_chat(model=model, gemini_payload=gemini_payload)
            except VertexError as e:
                try:
                    save_error_snapshot(
                        downstream_payload={"model": model, "stream": False, "body": body},
                        upstream_payload={"gemini_payload": gemini_payload},
                        upstream_response=getattr(e, "upstream_response", "") or e.message,
                        error_type=f"oai_nonstream_{type(e).__name__}",
                    )
                except Exception:
                    pass
                raise
            process_time = time.time() - start_time
            logger.success(f"[OpenAI] 完成: 模型={model}, 耗时={process_time:.3f}s")
            openai_resp = gemini_response_to_openai(gemini_resp, model, stream=False)
            return JSONResponse(content=openai_resp)

    app.post("/v1/chat/completions", response_model=None)(openai_chat_completions)

    logger.info("FastAPI 应用创建完成")
    return app


# ==================== 辅助函数 ====================

def _load_models_config() -> list[str]:
    try:
        with open(MODELS_CONFIG_FILE, 'r', encoding='utf-8') as f:
            config = json.load(f)
            return cast(list[str], config.get('models', []))
    except Exception:
        return ["gemini-2.5-pro", "gemini-2.5-flash", "gemini-2.5-flash-lite"]
