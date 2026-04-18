"""OpenAI 兼容层

将 OpenAI 格式的请求转换为 Gemini 格式，将 Gemini 格式的响应转换回 OpenAI 格式。
支持 /v1/chat/completions 和 /v1/models 端点。
"""

import asyncio
import json
import time
import uuid
from typing import Any, AsyncGenerator

from src.utils.logger import get_logger

logger = get_logger(__name__)


# ==================== 请求转换：OpenAI → Gemini ====================

def openai_messages_to_gemini(messages: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """
    将 OpenAI messages 列表转换为 Gemini contents 列表和 systemInstruction。
    
    Returns:
        (system_instruction, contents)
    """
    system_parts: list[str] = []
    contents: list[dict[str, Any]] = []

    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")

        if role == "system":
            # system message → systemInstruction
            if isinstance(content, str):
                system_parts.append(content)
            elif isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "text":
                        system_parts.append(part.get("text", ""))
            continue

        # OpenAI role → Gemini role
        gemini_role = "model" if role == "assistant" else "user"

        # 处理 content 为字符串或列表
        parts: list[dict[str, Any]] = []
        if isinstance(content, str):
            if content:
                parts.append({"text": content})
        elif isinstance(content, list):
            for part in content:
                if not isinstance(part, dict):
                    continue
                ptype = part.get("type")
                if ptype == "text":
                    text = part.get("text", "")
                    if text:
                        parts.append({"text": text})
                elif ptype == "image_url":
                    image_url = part.get("image_url", {})
                    url = image_url.get("url", "") if isinstance(image_url, dict) else str(image_url)
                    if url.startswith("data:"):
                        # base64 内嵌图片: data:<mime>;base64,<data>
                        try:
                            header, b64data = url.split(",", 1)
                            mime_type = header.split(":")[1].split(";")[0]
                            parts.append({"inlineData": {"mimeType": mime_type, "data": b64data}})
                        except Exception:
                            pass
                    else:
                        parts.append({"fileData": {"mimeType": "image/jpeg", "fileUri": url}})

        # 处理 tool_calls (function calling)
        tool_calls = msg.get("tool_calls")
        if tool_calls and isinstance(tool_calls, list):
            for tc in tool_calls:
                if not isinstance(tc, dict):
                    continue
                func = tc.get("function", {})
                try:
                    args = json.loads(func.get("arguments", "{}"))
                except Exception:
                    args = {}
                parts.append({
                    "functionCall": {
                        "name": func.get("name", ""),
                        "args": args
                    }
                })

        # 处理 tool_call_id (function response)
        if role == "tool":
            tool_call_id = msg.get("tool_call_id", "")
            name = msg.get("name", tool_call_id)
            response_content = content if isinstance(content, str) else json.dumps(content)
            parts = [{
                "functionResponse": {
                    "name": name,
                    "response": {"content": response_content}
                }
            }]
            gemini_role = "user"

        if parts:
            contents.append({"role": gemini_role, "parts": parts})

    system_instruction = None
    if system_parts:
        combined = "\n".join(system_parts)
        system_instruction = {"parts": [{"text": combined}]}

    return system_instruction, contents


def openai_request_to_gemini(body: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """
    将完整的 OpenAI 请求体转换为 (model, gemini_payload)。
    """
    model = body.get("model", "gemini-2.5-flash")
    messages = body.get("messages", [])

    system_instruction, contents = openai_messages_to_gemini(messages)

    gemini_payload: dict[str, Any] = {"contents": contents}

    if system_instruction:
        gemini_payload["systemInstruction"] = system_instruction

    # 生成参数转换
    gen_config: dict[str, Any] = {}
    if "temperature" in body:
        gen_config["temperature"] = body["temperature"]
    if "max_tokens" in body:
        gen_config["maxOutputTokens"] = body["max_tokens"]
    if "top_p" in body:
        gen_config["topP"] = body["top_p"]
    if "top_k" in body:
        gen_config["topK"] = body["top_k"]
    if "stop" in body:
        stops = body["stop"]
        gen_config["stopSequences"] = [stops] if isinstance(stops, str) else stops
    if "n" in body:
        gen_config["candidateCount"] = body["n"]
    if "response_format" in body:
        fmt = body["response_format"]
        if isinstance(fmt, dict) and fmt.get("type") == "json_object":
            gen_config["responseMimeType"] = "application/json"

    if gen_config:
        gemini_payload["generationConfig"] = gen_config

    # tools 转换 (OpenAI functions → Gemini tools)
    tools = body.get("tools")
    if tools and isinstance(tools, list):
        func_declarations: list[dict[str, Any]] = []
        for tool in tools:
            if not isinstance(tool, dict):
                continue
            if tool.get("type") == "function":
                func = tool.get("function", {})
                decl: dict[str, Any] = {"name": func.get("name", "")}
                if "description" in func:
                    decl["description"] = func["description"]
                if "parameters" in func:
                    decl["parameters"] = func["parameters"]
                func_declarations.append(decl)
        if func_declarations:
            gemini_payload["tools"] = [{"functionDeclarations": func_declarations}]

    # 旧版 functions 支持
    functions = body.get("functions")
    if functions and isinstance(functions, list) and "tools" not in gemini_payload:
        func_declarations = []
        for func in functions:
            if not isinstance(func, dict):
                continue
            decl = {"name": func.get("name", "")}
            if "description" in func:
                decl["description"] = func["description"]
            if "parameters" in func:
                decl["parameters"] = func["parameters"]
            func_declarations.append(decl)
        if func_declarations:
            gemini_payload["tools"] = [{"functionDeclarations": func_declarations}]

    return model, gemini_payload


# ==================== 响应转换：Gemini → OpenAI ====================

def gemini_response_to_openai(gemini_resp: dict[str, Any], model: str, stream: bool = False) -> dict[str, Any]:
    """
    将 Gemini 格式的响应转换为 OpenAI 格式。
    """
    completion_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
    created = int(time.time())

    candidates = gemini_resp.get("candidates", [])
    choices: list[dict[str, Any]] = []

    for idx, candidate in enumerate(candidates):
        content_obj = candidate.get("content", {})
        parts = content_obj.get("parts", []) if isinstance(content_obj, dict) else []
        finish_reason = candidate.get("finishReason", "stop").lower()
        if finish_reason == "stop":
            finish_reason = "stop"
        elif finish_reason in ("max_tokens", "length"):
            finish_reason = "length"
        else:
            finish_reason = "stop"

        # 提取文本内容和 function calls
        text_parts: list[str] = []
        tool_calls: list[dict[str, Any]] = []

        for part in parts:
            if not isinstance(part, dict):
                continue
            if "text" in part and not part.get("thought"):
                text_parts.append(part["text"])
            if "functionCall" in part:
                fc = part["functionCall"]
                tool_calls.append({
                    "id": f"call_{uuid.uuid4().hex[:16]}",
                    "type": "function",
                    "function": {
                        "name": fc.get("name", ""),
                        "arguments": json.dumps(fc.get("args", {}), ensure_ascii=False)
                    }
                })

        message: dict[str, Any] = {"role": "assistant", "content": "\n".join(text_parts) if text_parts else None}
        if tool_calls:
            message["tool_calls"] = tool_calls
            message["content"] = None
            finish_reason = "tool_calls"

        if stream:
            choices.append({
                "index": idx,
                "delta": message,
                "finish_reason": None
            })
        else:
            choices.append({
                "index": idx,
                "message": message,
                "finish_reason": finish_reason,
                "logprobs": None
            })

    # usage 统计
    usage_meta = gemini_resp.get("usageMetadata", {})
    usage = {
        "prompt_tokens": usage_meta.get("promptTokenCount", 0),
        "completion_tokens": usage_meta.get("candidatesTokenCount", 0),
        "total_tokens": usage_meta.get("totalTokenCount", 0)
    }

    obj_type = "chat.completion.chunk" if stream else "chat.completion"
    return {
        "id": completion_id,
        "object": obj_type,
        "created": created,
        "model": model,
        "choices": choices,
        "usage": usage if not stream else None,
        "system_fingerprint": None
    }


def gemini_sse_chunk_to_openai(
    gemini_chunk_json: dict[str, Any],
    model: str,
    completion_id: str,
    created: int
) -> list[str]:
    """
    将单个 Gemini SSE chunk (已解析的 JSON) 转换为 OpenAI SSE 格式字符串列表。
    返回空列表表示跳过该 chunk。若 chunk 同时含内容和 finish_reason，则拆为两条。
    """
    candidates = gemini_chunk_json.get("candidates", [])
    if not candidates:
        return []

    choices: list[dict[str, Any]] = []
    for idx, candidate in enumerate(candidates):
        content_obj = candidate.get("content", {})
        parts = content_obj.get("parts", []) if isinstance(content_obj, dict) else []
        finish_reason_raw = candidate.get("finishReason")
        finish_reason = None
        if finish_reason_raw:
            fr = finish_reason_raw.lower()
            if fr == "stop":
                finish_reason = "stop"
            elif fr in ("max_tokens", "length", "max_output_tokens"):
                finish_reason = "length"
            elif fr == "tool_calls":
                finish_reason = "tool_calls"
            else:
                finish_reason = "stop"

        text_parts: list[str] = []
        tool_calls: list[dict[str, Any]] = []

        for part in parts:
            if not isinstance(part, dict):
                continue
            if "text" in part and not part.get("thought"):
                text_parts.append(part["text"])
            if "functionCall" in part:
                fc = part["functionCall"]
                tool_calls.append({
                    "id": f"call_{uuid.uuid4().hex[:16]}",
                    "type": "function",
                    "function": {
                        "name": fc.get("name", ""),
                        "arguments": json.dumps(fc.get("args", {}), ensure_ascii=False)
                    }
                })

        delta: dict[str, Any] = {}
        if text_parts:
            delta["content"] = "".join(text_parts)
        elif tool_calls:
            delta["tool_calls"] = tool_calls
            finish_reason = "tool_calls"

        choices.append({
            "index": idx,
            "delta": delta,
            "finish_reason": finish_reason,
            "logprobs": None
        })

    if not any(c["delta"] for c in choices) and not any(c["finish_reason"] for c in choices):
        return []

    results: list[str] = []

    has_content = any(c["delta"] for c in choices)
    has_finish = any(c["finish_reason"] for c in choices)

    if has_content and has_finish:
        content_choices = [
            {**c, "finish_reason": None} for c in choices
        ]
        content_chunk = {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": content_choices,
            "system_fingerprint": None
        }
        results.append(f"data: {json.dumps(content_chunk, ensure_ascii=False)}\n\n")

        finish_choices = [
            {"index": c["index"], "delta": {}, "finish_reason": c["finish_reason"], "logprobs": None}
            for c in choices
        ]
        finish_chunk = {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": finish_choices,
            "system_fingerprint": None
        }
        results.append(f"data: {json.dumps(finish_chunk, ensure_ascii=False)}\n\n")
    else:
        chunk = {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": choices,
            "system_fingerprint": None
        }
        results.append(f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n")

    return results


async def stream_gemini_as_openai(
    gemini_stream: AsyncGenerator[str, None],
    model: str,
    fake_stream: bool = False
) -> AsyncGenerator[str, None]:
    """
    将 Gemini SSE 流转换为 OpenAI SSE 流。
    fake_stream=True 时：收到完整内容后拆成小块逐字发出，模拟流式输出体验。
    """
    completion_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
    created = int(time.time())

    # 发送首个 chunk（角色声明）
    first_chunk = {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}, "finish_reason": None}],
        "system_fingerprint": None
    }
    yield f"data: {json.dumps(first_chunk, ensure_ascii=False)}\n\n"

    async for raw_chunk in gemini_stream:
        # Gemini SSE 格式: "data: {...}\n\n"
        for line in raw_chunk.splitlines():
            line = line.strip()
            if not line.startswith("data:"):
                continue
            json_str = line[5:].strip()
            if not json_str or json_str == "[DONE]":
                continue
            try:
                gemini_json = json.loads(json_str)
            except json.JSONDecodeError:
                continue

            openai_lines = gemini_sse_chunk_to_openai(gemini_json, model, completion_id, created)
            for openai_line in openai_lines:
                if fake_stream:
                    # 假流式：解析内容块，把文字拆成每 3 个字符一组逐块发出
                    try:
                        data_str = openai_line[6:].strip()
                        chunk_obj = json.loads(data_str)
                        delta = chunk_obj.get("choices", [{}])[0].get("delta", {})
                        content = delta.get("content", "")
                        finish = chunk_obj.get("choices", [{}])[0].get("finish_reason")
                        if content and not finish:
                            TOKEN_SIZE = 3
                            for i in range(0, len(content), TOKEN_SIZE):
                                token = content[i:i + TOKEN_SIZE]
                                token_chunk = {
                                    "id": completion_id,
                                    "object": "chat.completion.chunk",
                                    "created": created,
                                    "model": model,
                                    "choices": [{"index": 0, "delta": {"content": token}, "finish_reason": None, "logprobs": None}],
                                    "system_fingerprint": None
                                }
                                yield f"data: {json.dumps(token_chunk, ensure_ascii=False)}\n\n"
                                await asyncio.sleep(0)
                            continue
                    except Exception:
                        pass
                yield openai_line

    yield "data: [DONE]\n\n"
