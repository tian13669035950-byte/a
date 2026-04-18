"""Vertex AI客户端"""

import asyncio
import json
import time
from typing import Any, cast, AsyncGenerator
from src.core.config import load_config

from src.core.errors import (
    VertexError,
    AuthenticationError,
    RateLimitError,
    InternalError,
    EmptyResponseError,
    parse_error_response,
    raise_for_status,
)
from src.stream import get_stream_processor
from src.utils.logger import get_logger

# 从拆分的模块导入
from .model_config import ModelConfigBuilder
from .transform import RequestTransformer, ResponseAggregator
from .network import NetworkClient

# 初始化日志
logger = get_logger(__name__)


class VertexAIClient:
    """Vertex AI API客户端 (Anonymous 模式)"""
    
    def __init__(self):
        logger.info("初始化 Vertex AI 客户端")
        
        # 加载配置
        self.config = load_config()
        self.max_retries = self.config.get("max_retries", 10) # 增大重试次数，匹配大香蕉
        
        # 初始化组件
        self.model_builder = ModelConfigBuilder()
        self.transformer = RequestTransformer(self.model_builder)
        self.aggregator = ResponseAggregator()
        self.network = NetworkClient()
        
        # 匿名接口基础 URL
        self.vertex_ai_anonymous_base_api = "https://cloudconsole-pa.clients6.google.com"
        
        logger.success("Vertex AI 客户端初始化完成")

    async def close(self):
        """关闭客户端并释放资源"""
        await self.network.close()

    @staticmethod
    async def _rotate_with_refresh() -> bool:
        """
        切换到下一个代理节点。
        若已转满一圈（所有节点都试过），自动重拉订阅并从头开始。
        返回 True 表示成功切换（可能是切换节点 or 刷新后首个节点）。
        """
        try:
            from proxy_manager import proxy_state as _ps
            if _ps.get_node_count() <= 0:
                return False

            rotated = _ps.rotate_to_next()

            if _ps.needs_refresh():
                logger.warning("所有节点已轮换一圈，自动重拉订阅获取新节点列表…")
                try:
                    from proxy_manager.routes import SUB_URL
                    from proxy_manager.subscription import fetch_and_parse
                    from proxy_manager.xray_manager import start_xray
                    new_nodes = await asyncio.to_thread(fetch_and_parse, SUB_URL)
                    if new_nodes:
                        _ps.set_nodes(new_nodes)   # 重置节点列表 + 计数
                        ok, _ = start_xray(new_nodes[0])
                        if ok:
                            logger.success(f"订阅刷新成功，共 {len(new_nodes)} 个节点，已切到第 1 个")
                            return True
                        logger.warning("订阅刷新后第一个节点启动失败，继续用旧节点")
                    else:
                        logger.warning("订阅刷新返回空节点，继续用现有列表")
                        _ps.reset_rotation_count()
                except Exception as ex:
                    logger.warning(f"订阅自动刷新失败: {ex}，继续使用现有节点")
                    _ps.reset_rotation_count()

            return rotated
        except Exception:
            return False

    async def complete_chat(self, model: str, gemini_payload: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        """聚合流式响应为非流式 ChatCompletion 对象"""
        _raw_image_response = kwargs.pop('_raw_image_response', False)
        return await self.aggregator.aggregate_stream(
            self.stream_chat(model, gemini_payload=gemini_payload, **kwargs),
            _raw_image_response=_raw_image_response
        )

    async def stream_chat(self, model: str, gemini_payload: dict[str, Any], **kwargs: Any) -> AsyncGenerator[str, Any]:
        """流式聊天（聚合后单 chunk 输出，兼容老路径）"""
        logger.info(f"开始流式聊天请求: 模型={model}")
        async for chunk in self._stream_chat_inner(model, gemini_payload=gemini_payload, **kwargs):
            yield chunk

    async def stream_chat_realtime(self, model: str, gemini_payload: dict[str, Any], **kwargs: Any) -> AsyncGenerator[dict[str, Any], None]:
        """真流式聊天：逐 result 块 yield Gemini 格式 dict（不聚合）"""
        logger.info(f"开始真流式聊天请求: 模型={model}")
        async for chunk in self._stream_realtime_inner(model, gemini_payload=gemini_payload, **kwargs):
            yield chunk

    async def _execute_single_attempt(
        self,
        session: Any,
        model: str,
        gemini_payload: dict[str, Any],
        recaptcha_token: str,
        attempt: int,
        kwargs: dict[str, Any],
        is_first_auth_attempt: bool = False
    ):
        """执行单次请求尝试"""
        
        # 1. 构建匿名接口特有的 GraphQL Context
        # 我们使用 transformer 处理原始 gemini payload 的 variables 转换
        dummy_original_body = {"variables": {}}
        new_variables = self.transformer.build_vertex_payload(
            model=model,
            gemini_payload=gemini_payload,
            original_body=dummy_original_body,
            kwargs=kwargs
        )['variables']
        
        # 注入匿名接口特有的字段
        new_variables["region"] = "global"
        new_variables["recaptchaToken"] = recaptcha_token

        new_body = {
            "querySignature": "2/l8eCsMMY49imcDQ/lwwXyL8cYtTjxZBF2dNqy69LodY=",
            "operationName": "StreamGenerateContentAnonymous",
            "variables": new_variables,
        }
        
        downstream_payload: dict[str, Any] = {
            "model": model,
            "gemini_payload": gemini_payload,
            "kwargs": {k: v for k, v in kwargs.items() if k != 'tools'},
            "attempt": attempt,
            "instance_id": "anonymous"
        }
        
        stream_processor = get_stream_processor()
        stream_processor.set_request_context(downstream_payload, new_body)
        
        headers = {
            "referer": "https://console.cloud.google.com/",
            "Content-Type": "application/json",
        }
        
        url = f"{self.vertex_ai_anonymous_base_api}/v3/entityServices/AiplatformEntityService/schemas/AIPLATFORM_GRAPHQL:batchGraphql?key=AIzaSyCI-zsRP85UVOi0DjtiCwWBwQ1djDy741g&prettyPrint=false"
        
        logger.debug(f"准备发送请求到: {url[:50]}...")
        
        # 2. 发送请求
        if attempt > 0 or not is_first_auth_attempt:
             logger.debug_json("发送 Vertex AI 请求体", new_body)
        
        async for response in self.network.stream_request(session, 'POST', url, headers=headers, json_data=new_body):
            # 3. 处理 HTTP 错误
            if response.status_code != 200:
                error_text = await response.aread()
                error_text_str = error_text.decode() if error_text else ""
                
                # 修改：如果是已知的第一次必败尝试，降低日志级别
                if is_first_auth_attempt and (response.status_code in [401, 403] or "Failed to verify action" in error_text_str):
                    logger.debug(f"上游服务返回预期内的首次认证失败: HTTP {response.status_code}")
                else:
                    logger.error(f"上游服务返回错误: HTTP {response.status_code}")
                    logger.debug_large("完整上游错误响应", error_text_str)
                
                # 特殊错误处理：Google 匿名接口常见反爬/失效情况
                if response.status_code in [401, 403] or "Failed to verify action" in error_text_str or "The caller does not have permission" in error_text_str:
                    raise AuthenticationError(
                        message=f"Authentication/Recaptcha failed: {error_text_str}",
                        details={"upstream_response": error_text_str},
                        upstream_response=error_text_str
                    )
                
                parsed_error = parse_error_response(error_text_str)
                if parsed_error:
                    parsed_error.upstream_response = error_text_str
                    raise parsed_error
                else:
                    raise raise_for_status(
                        code=response.status_code,
                        message=f"Upstream Error: {error_text_str}",
                        upstream_response=error_text_str
                    )
            
            # 4. 处理正常响应流
            logger.debug("开始处理流式响应")
            chunk_count = 0
            full_response_content: list[dict[str, Any]] = []
            has_auth_error_in_stream = False
            
            # 使用 curl_cffi 的 aiter_lines
            async def line_iterator():
                async for line in response.aiter_lines():
                    decoded_line = line.decode('utf-8') if isinstance(line, bytes) else line
                    yield decoded_line

            try:
                async for sse_event in stream_processor.process_stream(line_iterator(), model=model):

                    chunk_count += 1
                    try:
                        chunk_str = str(sse_event)
                        if chunk_str.strip().startswith("data: "):
                             data_str = chunk_str.strip()[6:]
                             data_obj = json.loads(data_str)
                             full_response_content.append(data_obj)
                    except Exception:
                        pass
                    yield sse_event
            except VertexError as e:
                # 即使是 parser 捕获到的 AuthenticationError 以外的其他 VertexError，如果带有 Failed to verify action 特征
                # 也要手动转换为 AuthenticationError 往上抛出
                if isinstance(e, AuthenticationError) or "Failed to verify action" in str(e) or "The caller does not have permission" in str(e):
                    raise AuthenticationError(
                        message=f"Authentication/Recaptcha failed in parser: {e}",
                        details={"upstream_response": str(e)},
                        upstream_response=str(e)
                    )
                else:
                    # 其他 VertexError 正常返回，或根据逻辑处理
                    yield e.to_sse()
                    return
            
            if full_response_content:
                 logger.debug_json("完整上游响应摘要", full_response_content)
            
            logger.success(f"流式响应处理完成，共处理 {chunk_count} 个数据块")
            return

    @staticmethod
    def _extract_text_from_sse_chunks(chunks: list[str]) -> str:
        """从 SSE chunk 列表中提取实际文字内容"""
        text_parts: list[str] = []
        for chunk_str in chunks:
            actual = chunk_str.strip()
            if actual.startswith("data: "):
                actual = actual[6:]
            if not actual:
                continue
            try:
                obj = json.loads(actual)
                for candidate in obj.get("candidates", []):
                    content = candidate.get("content", {})
                    for part in content.get("parts", []):
                        if isinstance(part, dict) and "text" in part and not part.get("thought"):
                            text_parts.append(part["text"])
            except Exception:
                pass
        return "".join(text_parts)

    @staticmethod
    def _extract_text_from_dict_chunks(chunks: list[dict[str, Any]]) -> str:
        """从 Gemini dict chunk 列表中提取文字内容（用于真流式空回复检测）"""
        out: list[str] = []
        for chunk in chunks:
            for cand in chunk.get("candidates", []) or []:
                content = cand.get("content", {}) or {}
                for part in content.get("parts", []) or []:
                    if isinstance(part, dict) and "text" in part and not part.get("thought"):
                        out.append(part["text"])
        return "".join(out)

    async def _process_streaming_object(self, obj: dict[str, Any]) -> AsyncGenerator[dict[str, Any], None]:
        """从单个上游 JSON 对象提取增量 Gemini chunk"""
        results = obj.get("results", []) or []
        for result in results:
            errors = result.get("errors")
            if errors and isinstance(errors, list) and len(errors) > 0:
                first_err = errors[0] if isinstance(errors[0], dict) else {"message": str(errors[0])}
                err_msg = first_err.get("message", "")
                if "Failed to verify action" in err_msg or "The caller does not have permission" in err_msg:
                    raise AuthenticationError(message=err_msg, upstream_response=err_msg)
                parsed = parse_error_response({"errors": errors})
                if parsed:
                    raise parsed

            data = result.get("data")
            if not isinstance(data, dict):
                continue

            ui = data.get("ui", {})
            if isinstance(ui, dict) and "streamGenerateContentAnonymous" in ui:
                inner = ui["streamGenerateContentAnonymous"]
                if isinstance(inner, dict):
                    data = inner
                elif isinstance(inner, list):
                    for item in inner:
                        if isinstance(item, dict) and item.get("candidates"):
                            yield item
                    continue
                else:
                    continue

            candidates = data.get("candidates", [])
            if candidates:
                chunk: dict[str, Any] = {"candidates": candidates}
                if data.get("usageMetadata"):
                    chunk["usageMetadata"] = data["usageMetadata"]
                if data.get("modelVersion"):
                    chunk["modelVersion"] = data["modelVersion"]
                if data.get("responseId"):
                    chunk["responseId"] = data["responseId"]
                yield chunk

    async def _execute_realtime_attempt(
        self,
        session: Any,
        model: str,
        gemini_payload: dict[str, Any],
        recaptcha_token: str,
        attempt: int,
        kwargs: dict[str, Any],
        is_first_auth_attempt: bool = False
    ) -> AsyncGenerator[dict[str, Any], None]:
        """真流式：执行单次请求，按 result 块 yield Gemini dict"""
        # 构建 payload，逻辑与 _execute_single_attempt 完全一致
        dummy_original_body = {"variables": {}}
        new_variables = self.transformer.build_vertex_payload(
            model=model,
            gemini_payload=gemini_payload,
            original_body=dummy_original_body,
            kwargs=kwargs
        )['variables']
        new_variables["region"] = "global"
        new_variables["recaptchaToken"] = recaptcha_token

        new_body = {
            "querySignature": "2/l8eCsMMY49imcDQ/lwwXyL8cYtTjxZBF2dNqy69LodY=",
            "operationName": "StreamGenerateContentAnonymous",
            "variables": new_variables,
        }

        # 也注入 stream_processor 的请求上下文（保留 token 计数能力）
        downstream_payload: dict[str, Any] = {
            "model": model,
            "gemini_payload": gemini_payload,
            "kwargs": {k: v for k, v in kwargs.items() if k != 'tools'},
            "attempt": attempt,
            "instance_id": "anonymous"
        }
        get_stream_processor().set_request_context(downstream_payload, new_body)

        headers = {
            "referer": "https://console.cloud.google.com/",
            "Content-Type": "application/json",
        }
        url = f"{self.vertex_ai_anonymous_base_api}/v3/entityServices/AiplatformEntityService/schemas/AIPLATFORM_GRAPHQL:batchGraphql?key=AIzaSyCI-zsRP85UVOi0DjtiCwWBwQ1djDy741g&prettyPrint=false"

        if attempt > 0 or not is_first_auth_attempt:
            logger.debug_json("发送 Vertex AI 请求体（真流式）", new_body)

        # 收集上游字节（上游一次性返回 JSON 数组），完成后逐 result yield
        async for response in self.network.stream_request(session, 'POST', url, headers=headers, json_data=new_body):
            if response.status_code != 200:
                error_text = await response.aread()
                error_text_str = error_text.decode() if error_text else ""
                if is_first_auth_attempt and (response.status_code in [401, 403] or "Failed to verify action" in error_text_str):
                    logger.debug(f"真流式：预期内首次认证失败 HTTP {response.status_code}")
                else:
                    logger.error(f"真流式：上游错误 HTTP {response.status_code}")
                    logger.debug_large("完整上游错误响应", error_text_str)

                if response.status_code in [401, 403] or "Failed to verify action" in error_text_str or "The caller does not have permission" in error_text_str:
                    raise AuthenticationError(
                        message=f"Authentication/Recaptcha failed: {error_text_str}",
                        details={"upstream_response": error_text_str},
                        upstream_response=error_text_str
                    )
                parsed_error = parse_error_response(error_text_str)
                if parsed_error:
                    parsed_error.upstream_response = error_text_str
                    raise parsed_error
                raise raise_for_status(
                    code=response.status_code,
                    message=f"Upstream Error: {error_text_str}",
                    upstream_response=error_text_str
                )

            raw_chunks: list[str] = []
            async for line in response.aiter_lines():
                decoded = line.decode('utf-8') if isinstance(line, bytes) else line
                raw_chunks.append(decoded)
            raw_data = '\n'.join(raw_chunks)

            if not raw_data:
                raise EmptyResponseError("Upstream returned no data (realtime)")

            try:
                data_list = json.loads(raw_data)
                if not isinstance(data_list, list):
                    data_list = [data_list]
            except json.JSONDecodeError:
                logger.warning("真流式：JSON 解析失败，尝试容错")
                data_list = []

            logger.debug(f"真流式：解析到 {len(data_list)} 个顶层对象")
            for obj in data_list:
                if isinstance(obj, dict):
                    async for chunk in self._process_streaming_object(obj):
                        yield chunk
            return

    async def _stream_realtime_inner(self, model: str, gemini_payload: dict[str, Any], **kwargs: Any) -> AsyncGenerator[dict[str, Any], None]:
        """真流式内部循环（含重试 / 节点切换 / 首次 401 重试），与 _stream_chat_inner 同构"""
        max_retries = self.max_retries
        quota_max_retries = 2
        quota_attempts = 0
        content_yielded = False
        recaptcha_token = None
        is_first_auth_attempt = True
        attempt = 0

        session = self.network.create_session()
        try:
            while attempt <= max_retries:
                if not recaptcha_token:
                    recaptcha_token = await self.network.fetch_recaptcha_token(session)
                    is_first_auth_attempt = True

                if not recaptcha_token:
                    if attempt == max_retries:
                        raise AuthenticationError("Could not fetch recaptcha token.")
                    attempt += 1
                    await asyncio.sleep(1)
                    continue

                logger.debug(f"真流式：第 {attempt + 1}/{max_retries + 1} 次尝试 {'(首次认证)' if is_first_auth_attempt else ''}")

                try:
                    # 先收一遍判断是否空回复
                    buffered: list[dict[str, Any]] = []
                    async for ch in self._execute_realtime_attempt(
                        session, model, gemini_payload, recaptcha_token, attempt, kwargs,
                        is_first_auth_attempt=is_first_auth_attempt
                    ):
                        buffered.append(ch)

                    actual_text = self._extract_text_from_dict_chunks(buffered)
                    if not actual_text.strip() and attempt < max_retries:
                        logger.warning(f"真流式：空回复，自动重试（第 {attempt + 1} 次）")
                        switched = await self._rotate_with_refresh()
                        if switched:
                            logger.info("真流式：已自动切换到下一个代理节点")
                            await asyncio.sleep(2)
                        recaptcha_token = None
                        attempt += 1
                        continue

                    if not actual_text.strip():
                        logger.warning(f"真流式：重试耗尽仍空，原样发送（模型={model}）")

                    for ch in buffered:
                        yield ch
                        content_yielded = True
                    break

                except AuthenticationError as e:
                    if is_first_auth_attempt:
                        logger.debug("真流式：首次 401/403 重试")
                        is_first_auth_attempt = False
                        await asyncio.sleep(0.5)
                        continue
                    logger.warning(f"真流式：认证错误 {e.message}")
                    recaptcha_token = None
                    if content_yielded or attempt >= max_retries:
                        raise
                    attempt += 1
                    await asyncio.sleep(1)
                    continue

                except RateLimitError as e:
                    logger.warning(f"真流式：限流 {e.message}")
                    quota_attempts += 1
                    if content_yielded or quota_attempts > quota_max_retries or attempt >= max_retries:
                        logger.error(f"真流式：配额重试已达上限 ({quota_attempts}/{quota_max_retries})")
                        raise
                    rotated = await self._rotate_with_refresh()
                    wait_time = 3 if rotated else (e.retry_after if e.retry_after else min(10, 2 ** attempt + 1))
                    logger.info(f"真流式：等待 {wait_time}s 后重试 ({quota_attempts}/{quota_max_retries})")
                    attempt += 1
                    await asyncio.sleep(wait_time)
                    continue

                except VertexError as e:
                    logger.error(f"真流式：Vertex 错误 {e.message}")
                    if not e.is_retryable or content_yielded or attempt >= max_retries:
                        raise
                    wait_time = min(15, 1.5 ** attempt)
                    logger.info(f"真流式：可重试错误，等待 {wait_time:.1f}s")
                    attempt += 1
                    await asyncio.sleep(wait_time)
                    continue

                except Exception as e:
                    logger.error(f"真流式：未预期异常 {e}")
                    if content_yielded or attempt >= max_retries:
                        raise InternalError(message=f"Internal error: {e}")
                    attempt += 1
                    await asyncio.sleep(1)
                    continue
        finally:
            await session.close()

    async def _stream_chat_inner(self, model: str, gemini_payload: dict[str, Any], **kwargs: Any) -> AsyncGenerator[str, Any]:
        """实际的流式聊天逻辑（内部方法）"""
        max_retries = self.max_retries
        # 配额耗尽专用重试上限：换 IP 通常救不回 per-project 配额，硬重试只是浪费时间
        quota_max_retries = 2
        quota_attempts = 0
        content_yielded = False
        
        logger.debug(f"开始内部流式聊天，最大重试次数: {max_retries}")

        # 使用同一个 Session 维持可能的状态，并且实现现抓现用
        # 匿名 Token 第一次使用必定 401/403，需要处理
        recaptcha_token = None
        is_first_auth_attempt = True
        attempt = 0
        
        # 每个请求任务创建一个独立的 Session 并复用它
        session = self.network.create_session()
        try:
            while attempt <= max_retries:
                # 1. 获取 Recaptcha Token
                if not recaptcha_token:
                    recaptcha_token = await self.network.fetch_recaptcha_token(session)
                    is_first_auth_attempt = True
                
                if not recaptcha_token:
                    if attempt == max_retries:
                        yield AuthenticationError("Could not fetch recaptcha token.").to_sse()
                        return
                    attempt += 1
                    await asyncio.sleep(1)
                    continue
                
                logger.debug(f"尝试第 {attempt + 1}/{max_retries + 1} 次正式请求 {'(首次认证重试)' if is_first_auth_attempt else ''}")
                
                try:
                    # 2. 先缓冲本次响应，确认有实际文字内容再发给客户端
                    buffered_chunks: list[str] = []
                    async for chunk in self._execute_single_attempt(
                        session, model, gemini_payload, recaptcha_token, attempt, kwargs,
                        is_first_auth_attempt=is_first_auth_attempt
                    ):
                        buffered_chunks.append(chunk)

                    # 3. 检查是否有真实文字内容
                    actual_text = self._extract_text_from_sse_chunks(buffered_chunks)
                    if not actual_text.strip() and attempt < max_retries:
                        logger.warning(f"收到空回复（无文字内容），自动重试（第 {attempt + 1} 次）")
                        # 尝试换一个代理节点再试（转满一圈自动重拉订阅）
                        switched = await self._rotate_with_refresh()
                        if switched:
                            logger.info("空回复，已自动切换到下一个代理节点")
                            await asyncio.sleep(2)
                        recaptcha_token = None  # 强制重新拿 token
                        attempt += 1
                        continue

                    # 4. 有内容（或已到重试上限）→ 全部发给客户端
                    if not actual_text.strip():
                        logger.warning(f"重试耗尽，仍为空回复，原样发送（模型={model}）")
                    for chunk in buffered_chunks:
                        yield chunk
                        content_yielded = True
                    
                    # 请求成功完成
                    break
                
                except AuthenticationError as e:
                    # 处理“匿名 Token 第一次使用必定失败”的特性
                    if is_first_auth_attempt:
                        logger.debug("触发首次认证重试机制 (预期内的 401/403)")
                        is_first_auth_attempt = False
                        # 保持同一个 token 重试一次
                        await asyncio.sleep(0.5)
                        continue
                    
                    # 如果不是第一次尝试依然认证失败，说明 Token 可能失效
                    logger.warning(f"认证/Recaptcha错误: {e.message}")
                    recaptcha_token = None # 清除 Token 触发下一次循环重新获取
                    
                    if content_yielded:
                        logger.error("已产生内容，无法安全重试")
                        yield e.to_sse()
                        return
                    
                    if attempt < max_retries:
                        attempt += 1
                        await asyncio.sleep(1)
                        continue
                    else:
                        logger.error("重试次数耗尽")
                        yield e.to_sse()
                        return
                
                except RateLimitError as e:
                    logger.warning(f"限流错误: {e.message}")
                    quota_attempts += 1
                    # 配额错误专用上限：试 quota_max_retries 次（换 IP）后还不行就放弃，
                    # 避免用户在酒馆等 1-2 分钟最后还是空回
                    if content_yielded or quota_attempts > quota_max_retries or attempt >= max_retries:
                        logger.error(f"配额重试已达上限 ({quota_attempts}/{quota_max_retries})，返回错误")
                        yield e.to_sse()
                        return

                    # 配额耗尽时，尝试自动切换到下一个代理节点（转满一圈自动重拉订阅）
                    rotated = await self._rotate_with_refresh()
                    if rotated:
                        logger.info(f"配额耗尽，已自动切换到下一个代理节点 ({quota_attempts}/{quota_max_retries})")

                    wait_time = 3 if rotated else (e.retry_after if e.retry_after else min(10, 2 ** attempt + 1))
                    logger.info(f"触发限流，等待 {wait_time}s 后重试 (第 {attempt + 1} 次重试)")
                    attempt += 1
                    await asyncio.sleep(wait_time)
                    continue
                
                except VertexError as e:
                    logger.error(f"Vertex 错误: {e.message}")
                    if not e.is_retryable or content_yielded or attempt >= max_retries:
                         yield e.to_sse()
                         return
                
                    # 针对 5xx 错误也加入轻量级指数退避
                    wait_time = min(15, 1.5 ** attempt)
                    logger.info(f"触发可重试 Vertex 错误，等待 {wait_time:.1f}s 后重试 (第 {attempt + 1} 次重试)")
                    attempt += 1
                    await asyncio.sleep(wait_time)
                    continue
                
                except Exception as e:
                    logger.error(f"未预期的异常: {e}")
                    if content_yielded or attempt >= max_retries:
                        yield InternalError(message=f"Internal error: {e}").to_sse()
                        return
                    
                    attempt += 1
                    await asyncio.sleep(1)
                    continue
        finally:
            await session.close()
