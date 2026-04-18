"""
流式响应处理器（简化版）

实现"假流式"：收集所有上游数据后，解析并聚合为一个包含完整上下文（思考过程+最终内容）的响应，一次性发送给下游。
"""

import json
import time
from typing import Any, cast
import collections.abc

from src.core.errors import (
    VertexError,
    EmptyResponseError,
    InternalError,
    NotFoundError,
    InvalidArgumentError,
    RateLimitError, 
)
from src.utils.logger import get_logger
from src.utils.token_counter import calculate_usage_metadata
from .parser import parse_upstream_data


# 初始化日志
logger = get_logger(__name__)


class StreamProcessor:
    """
    简化的流式响应处理器 (v3 - 假流式 + 思考过程)
    
    职责:
    1. 收集上游所有流式数据
    2. 使用 parser 解析聚合后的数据
    3. 构建 Gemini 格式的 SSE 响应
    """
    
    def __init__(self):
        logger.debug("初始化流处理器")
        
        # 状态追踪
        self._actual_content_sent = False
        self._request_context: dict[str, Any] = {}
    
    def has_actual_content_sent(self) -> bool:
        """检查是否已发送实际文本内容"""
        return self._actual_content_sent
    
    def set_request_context(self, downstream_payload: dict[str, Any], upstream_payload: dict[str, Any]):
        """设置请求上下文"""
        logger.debug("设置流处理器请求上下文")
        self._request_context = {
            'downstream_payload': downstream_payload,
            'upstream_payload': upstream_payload
        }
    
    def _create_gemini_chunk(
        self,
        parts: list[dict[str, Any]],
        finish_reason: str,
        safety_ratings: list[dict[str, Any]],
        citation_metadata: dict[str, Any],
        grounding_metadata: dict[str, Any],
        candidate_index: int,
        prompt_feedback: dict[str, Any],
        usage_metadata: dict[str, Any]
    ) -> str:
        """根据聚合后的内容，创建一个包含完整上下文的Gemini格式SSE事件。"""
        candidate: dict[str, Any] = {
            "finishReason": (finish_reason or "STOP").upper(),
            "index": candidate_index
        }
        
        if parts:
            candidate["content"] = {
                "parts": parts,
                "role": "model"
            }
        
        # 只添加实际存在的字段
        if safety_ratings:
            candidate["safetyRatings"] = safety_ratings
        if citation_metadata:
            candidate["citationMetadata"] = citation_metadata
        if grounding_metadata:
            candidate["groundingMetadata"] = grounding_metadata
        
        chunk: dict[str, Any] = {"candidates": [candidate]}
        
        # 只添加实际存在的顶层字段
        if prompt_feedback:
            chunk["promptFeedback"] = prompt_feedback
        if usage_metadata:
            chunk["usageMetadata"] = usage_metadata
            
        return "data: " + json.dumps(chunk, ensure_ascii=False) + "\n\n"

    async def process_stream(
        self,
        response_iterator: collections.abc.AsyncIterator[str],
        model: str = "vertex-ai-proxy"
    ) -> collections.abc.AsyncGenerator[str, None]:
        """
        处理流式响应（假流式 v3）。
        """
        # 移除重复日志，只在开始时记录一次
        # logger.info(f"开始处理流式响应: 模型={model}")
        start_time = time.time()
        raw_chunks: list[str] = []
        
        try:
            chunk_count = 0
            logger.debug("开始收集上游数据块")
            async for chunk in response_iterator:
                chunk_count += 1
                raw_chunks.append(chunk)
            
            logger.debug(f"收集完成，共 {chunk_count} 个数据块")
            raw_data = '\n'.join(raw_chunks)
            
            # 记录完整的原始上游响应
            try:
                parsed_data = json.loads(raw_data)
                logger.debug_json("完整原始上游响应", parsed_data)
            except json.JSONDecodeError:
                logger.debug_large("完整原始上游响应", raw_data)
            
            if not raw_data:
                logger.error("上游返回空数据")
                raise EmptyResponseError("Upstream returned no data")

            # 使用独立解析函数
            result = parse_upstream_data(raw_data)
            
            # 关键修复：在这里处理解析出的上游错误
            if result["has_error"] and not result["parts"]:
                error_msg = result["error_message"]
                if "Failed to verify action" not in error_msg and "The caller does not have permission" not in error_msg:
                    logger.error(f"API 错误且无内容: {error_msg}")
                
                # 如果 parser 已经解析出了错误对象，直接抛出
                if result.get("error_obj"):
                    raise result["error_obj"]

                # 降级处理：基于上游错误消息抛出错误
                error_msg_lower = error_msg.lower()
                if "not found" in error_msg_lower:
                    raise NotFoundError(message=error_msg)
                elif "resource has been exhausted" in error_msg_lower or "quota" in error_msg_lower:
                    raise RateLimitError(message=error_msg)
                elif "failed to verify action" in error_msg_lower or "the caller does not have permission" in error_msg_lower:
                    from src.core.errors import AuthenticationError
                    raise AuthenticationError(
                        message=f"Authentication/Recaptcha failed: {error_msg}",
                        details={"upstream_response": error_msg},
                        upstream_response=error_msg
                    )
                else:
                    raise InvalidArgumentError(message=error_msg)
            
            finish_reason = result.get("finish_reason") or "STOP"
            
            if not result["parts"] and finish_reason == "STOP" and not result["has_error"]:
                 if not result.get("prompt_feedback"):
                    logger.error("上游返回空响应 (无 parts 且 finish_reason=STOP)")
                    # 快照将由 VertexAIClient 统一保存
                    raise EmptyResponseError("Upstream returned empty response (STOP with no content/metadata)")
            
            # 计算 usage metadata
            usage_metadata = {}
            if self._request_context:
                try:
                    downstream_payload = self._request_context.get('downstream_payload', {})
                    
                    # 从请求上下文中提取输入内容
                    prompt_contents: list[dict[str, Any]] = []
                    if 'gemini_payload' in downstream_payload:
                        gemini_payload = downstream_payload['gemini_payload']
                        if isinstance(gemini_payload, dict) and 'contents' in gemini_payload:
                            prompt_contents = cast(list[dict[str, Any]], gemini_payload['contents'])
                    
                    # 计算 token 使用情况
                    usage_metadata = await calculate_usage_metadata(
                        prompt_contents=prompt_contents,
                        response_parts=result["parts"],
                        request_context=self._request_context
                    )
                except Exception as e:
                    logger.warning(f"计算 usage metadata 失败: {e}")
                    usage_metadata = {}
            
            final_chunk = self._create_gemini_chunk(
                parts=result["parts"],
                finish_reason=result["finish_reason"],
                safety_ratings=result["safety_ratings"],
                citation_metadata=result["citation_metadata"],
                grounding_metadata=result["grounding_metadata"],
                candidate_index=result["candidate_index"],
                prompt_feedback=result["prompt_feedback"],
                usage_metadata=usage_metadata
            )
            
            process_time = time.time() - start_time
            logger.success(f"流式响应处理完成: 耗时={process_time:.3f}s, 完成原因={result['finish_reason']}")
            
            yield final_chunk
            self._actual_content_sent = True
            
        except VertexError as e:
            if "Failed to verify action" in e.message or "The caller does not have permission" in e.message:
                from src.core.errors import AuthenticationError
                if not isinstance(e, AuthenticationError):
                    raise AuthenticationError(
                        message=f"Stream contained Authentication error: {e.message}",
                        details={"upstream_response": e.upstream_response or e.message},
                        upstream_response=e.upstream_response or e.message
                    )
            else:
                logger.error(f"流处理 Vertex 错误: {e.message}")
            raise
        except Exception as e:
            logger.error(f"流处理未知错误: {e}")
            raise InternalError(message=f"Unknown stream processing error: {e}")


def get_stream_processor() -> StreamProcessor:
    """创建流处理器实例"""
    return StreamProcessor()
