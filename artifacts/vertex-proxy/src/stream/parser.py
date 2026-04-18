"""
Vertex AI 响应解析工具函数

负责解析上游 API 的响应数据，处理错误和元数据提取。
"""

import json
from typing import Any, cast
from src.core.errors import (
    VertexError,
    InternalError,
    parse_error_response,
)
from src.utils.logger import get_logger

logger = get_logger(__name__)

def extract_path_index(result: dict[str, Any]) -> int:
    """从 result 对象中提取 path 索引"""
    path = result.get('path', [])
    if not path or not isinstance(path, list):
        return -1
    try:
        # 索引通常是路径的最后一个整数元素
        path_list = cast(list[Any], path)
        for elem in reversed(path_list):
            if isinstance(elem, int):
                return elem
            if isinstance(elem, str) and elem.isdigit():
                return int(elem)
    except (IndexError, ValueError, TypeError):
        pass
    return -1

def clean_json_string(raw_data: str) -> str:
    """清理并规范化 JSON 字符串"""
    cleaned_data = raw_data.strip()
    if cleaned_data.endswith(','):
        cleaned_data = cleaned_data[:-1]
    
    if not cleaned_data.startswith('['):
        cleaned_data = f'[{cleaned_data}]'
    elif not cleaned_data.endswith(']'):
         # 这是一个不完整的数组，尝试补全
         if '}]' not in cleaned_data:
            cleaned_data += ']'
    return cleaned_data

def process_candidate_metadata(candidate_data: dict[str, Any]) -> dict[str, Any]:
    """提取 candidate 级别的元数据（仅提取实际存在的字段）"""
    metadata: dict[str, Any] = {}
    
    # 只有当 finishReason 存在且是 STOP 时才提取
    finish_reason = candidate_data.get('finishReason')
    if finish_reason == 'STOP':
        metadata['finish_reason'] = 'STOP'
    
    # 提取实际存在的元数据字段
    if candidate_data.get('safetyRatings'):
        metadata['safety_ratings'] = candidate_data['safetyRatings']
        
    if candidate_data.get('citationMetadata'):
        metadata['citation_metadata'] = candidate_data['citationMetadata']
        
    if candidate_data.get('groundingMetadata'):
        metadata['grounding_metadata'] = candidate_data['groundingMetadata']
    
    # index 字段通常存在
    if candidate_data.get('index') is not None:
        metadata['candidate_index'] = candidate_data['index']
        
    return metadata

def _extract_error_message(item: dict[str, Any]) -> str | None:
    """从单个响应项中提取错误信息（如果有）"""
    
    # 1. 检查顶层 error 对象 (标准 Google Cloud 错误)
    error_obj = item.get('error')
    if error_obj:
        if isinstance(error_obj, dict):
            # 显式转换为 Dict 以满足类型检查
            safe_error_obj = cast(dict[str, Any], error_obj)
            return str(safe_error_obj.get('message', str(safe_error_obj)))
        return str(error_obj)

    # 2. 检查顶层 errors 列表 (GraphQL 风格或批处理错误)
    errors = item.get('errors')
    if errors and isinstance(errors, list):
        # 显式转换
        safe_errors = cast(list[Any], errors)
        if safe_errors:
            first_error = safe_errors[0]
            if isinstance(first_error, dict):
                safe_first_error = cast(dict[str, Any], first_error)
                return str(safe_first_error.get('message', str(safe_first_error)))
            return str(first_error)
            
    return None

def _clean_part_fields(part: dict[str, Any]) -> dict[str, Any]:
    """
    清理 part 中的空字段，只保留有实际内容的字段
    
    Args:
        part: 原始 part 字典
        
    Returns:
        清理后的 part 字典
    """
    try:
        from src.core.types import ContentPart
        content_part = ContentPart.model_validate(part)
        cleaned_part = content_part.model_dump(exclude_none=True, by_alias=True)
        # 额外处理 text 为空字符串的情况，以及一些需要保留真实值的嵌套字典
        if 'text' in cleaned_part and not str(cleaned_part['text']).strip():
            del cleaned_part['text']
        return cleaned_part
    except Exception as e:
        logger.debug(f"Pydantic 验证 part 失败，回退到基础清洗: {e}")
        cleaned_part: dict[str, Any] = {}
        
        # 处理文本内容
        if 'text' in part:
            text_value = part['text']
            if text_value and str(text_value).strip():
                cleaned_part['text'] = text_value
        
        # 处理思考标记
        if 'thought' in part:
            cleaned_part['thought'] = part['thought']
        
        # 处理数据类型标记（如果存在且不为空）
        if 'data' in part and part['data']:
            cleaned_part['data'] = part['data']
        
        # 处理函数调用（只保留有名称的）
        if 'functionCall' in part:
            func_call = part['functionCall']
            if isinstance(func_call, dict):
                func_call_dict = cast(dict[str, Any], func_call)
                if func_call_dict.get('name') and str(func_call_dict['name']).strip():
                    cleaned_part['functionCall'] = func_call
        
        # 处理函数响应（只保留有名称的）
        if 'functionResponse' in part:
            func_response = part['functionResponse']
            if isinstance(func_response, dict):
                func_response_dict = cast(dict[str, Any], func_response)
                if func_response_dict.get('name') and str(func_response_dict['name']).strip():
                    cleaned_part['functionResponse'] = func_response
        
        # 处理内联数据（只保留有实际数据的）
        if 'inlineData' in part:
            inline_data = part['inlineData']
            if isinstance(inline_data, dict):
                inline_data_dict = cast(dict[str, Any], inline_data)
                if (inline_data_dict.get('data') and
                    str(inline_data_dict['data']).strip() and
                    inline_data_dict.get('mimeType') and
                    str(inline_data_dict['mimeType']).strip()):
                    cleaned_part['inlineData'] = inline_data
        
        # 处理文件数据（只保留有实际数据的）
        if 'fileData' in part:
            file_data = part['fileData']
            if isinstance(file_data, dict):
                file_data_dict = cast(dict[str, Any], file_data)
                if (file_data_dict.get('fileUri') and
                    str(file_data_dict['fileUri']).strip() and
                    file_data_dict.get('mimeType') and
                    str(file_data_dict['mimeType']).strip()):
                    cleaned_part['fileData'] = file_data
        
        return cleaned_part

def _merge_content_blocks(parts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    合并思考块和非思考块的文本内容
    
    Args:
        parts: 原始的 parts 列表
        
    Returns:
        合并后的 parts 列表，思考块在前，非思考块在后
    """
    # 首先清理所有 parts 中的空字段
    cleaned_parts = [_clean_part_fields(part) for part in parts]
    # 过滤掉完全为空的 parts
    cleaned_parts = [part for part in cleaned_parts if part]
    
    thought_texts: list[str] = []  # 思考块文本
    content_texts: list[str] = []  # 非思考块文本
    other_parts: list[dict[str, Any]] = []  # 非文本部分（函数调用等）
    
    # 分类处理所有 parts
    for part in cleaned_parts:
            
        # 检查是否为文本块
        if 'text' in part and part['text']:
            text_content = str(part['text']).strip()
            if not text_content:
                continue
                
            # 判断是否为思考块
            is_thought = part.get('thought', False)
            
            if is_thought:
                thought_texts.append(text_content)
            else:
                content_texts.append(text_content)
        else:
            # 非文本部分（如函数调用、函数响应等）保持原样
            other_parts.append(part)
    
    # 构建合并后的 parts 列表
    merged_parts: list[dict[str, Any]] = []
    
    # 1. 合并思考块文本（如果有）
    if thought_texts:
        merged_thought_text = ''.join(thought_texts)
        merged_parts.append({
            'text': merged_thought_text,
            'thought': True
        })
    
    # 2. 添加非文本部分（保持原有顺序）
    merged_parts.extend(other_parts)
    
    # 3. 合并非思考块文本（如果有）
    if content_texts:
        merged_content_text = ''.join(content_texts)
        merged_parts.append({
            'text': merged_content_text
        })
    
    return merged_parts

def parse_upstream_data(raw_data: str) -> dict[str, Any]:
    """
    解析完整的上游原始数据。
    
    Returns:
        包含 parts, finish_reason 和实际存在的元数据的字典
    """
    state: dict[str, Any] = {
        "finish_reason": None,
        "safety_ratings": [],
        "citation_metadata": {},
        "grounding_metadata": {},
        "candidate_index": 0,
        "prompt_feedback": {},
        "has_error": False,
        "error_message": "",
        "error_obj": None,
        "parts_by_path": {},
        "unindexed_parts": []
    }

    try:
        cleaned_data = clean_json_string(raw_data)
        data_list = json.loads(cleaned_data)
        
        if not isinstance(data_list, list):
            data_list = [data_list]
        
        # 显式转换为 List[Any]
        safe_data_list = cast(list[Any], data_list)

        for item in safe_data_list:
            if not isinstance(item, dict):
                continue
            
            item_dict = cast(dict[str, Any], item)
            
            # 1. 优先检查顶层错误
            error_msg = _extract_error_message(item_dict)
            if error_msg:
                state["has_error"] = True
                state["error_message"] = error_msg
                # 继续解析以尝试提取更多上下文，但不应覆盖错误状态
            
            # 2. 处理 results 列表 (主要数据载体)
            results = item_dict.get('results', [])
            if not isinstance(results, list):
                continue
            
            typed_results: list[dict[str, Any]] = []
            safe_results = cast(list[Any], results)
            for r in safe_results:
                if isinstance(r, dict):
                    typed_results.append(cast(dict[str, Any], r))

            # 3. 检查 results 中的错误 (使用统一解析逻辑)
            parsed_error = parse_error_response(typed_results)
            if parsed_error:
                state["has_error"] = True
                state["error_message"] = parsed_error.message
                state["error_obj"] = parsed_error
            
            # 4. 提取数据 parts
            for result in typed_results:
                # 如果有 data=null 且有 errors，这已经被上面的 parsed_error 捕获
                # 我们跳过这个 result 的 data 处理，避免 NoneType 错误
                if result.get('data') is None and 'errors' in result:
                    continue

                path_index = extract_path_index(result)
                data = result.get('data')
                
                if isinstance(data, dict):
                    _update_state_from_data(state, cast(dict[str, Any], data), path_index)


    except json.JSONDecodeError as e:
        state["has_error"] = True
        state["error_message"] = f"JSON parse error: {e}"
    except VertexError:
        raise
    except Exception as e:
        # 捕获其他未预期的解析错误
        logger.error(f"解析过程发生未知错误: {e}")
        state["has_error"] = True
        state["error_message"] = f"Parse error: {str(e)}"
    
    # 组装 parts - 按 path_index 排序，每个 index 可能有多个 parts（列表）
    parts_by_path = cast(dict[int, list[Any]], state['parts_by_path'])
    ordered_parts: list[dict[str, Any]] = []
    for k in sorted(parts_by_path.keys()):
        parts_at_index = parts_by_path[k]
        if isinstance(parts_at_index, list):
            ordered_parts.extend(cast(list[dict[str, Any]], parts_at_index))
        else:
            ordered_parts.append(cast(dict[str, Any], parts_at_index))
    unindexed_parts = cast(list[Any], state['unindexed_parts'])
    ordered_parts.extend(unindexed_parts)
    
    # 新增：合并思考块和非思考块
    final_parts = _merge_content_blocks(ordered_parts)
    
    result: dict[str, Any] = {
        "parts": final_parts
    }
    # 将 state 中除了临时存储结构外的所有字段合并到 result
    excluded_keys = ['parts_by_path', 'unindexed_parts']
    result.update({k: v for k, v in state.items() if k not in excluded_keys})
    return result

def _update_state_from_data(state: dict[str, Any], data: dict[str, Any], path_index: int):
    """从数据对象更新解析状态（仅提取实际存在的字段）"""
    # 只提取实际存在的顶层元数据
    if data.get('promptFeedback'):
        state['prompt_feedback'] = data['promptFeedback']
            
    # 处理 candidates
    candidates = data.get('candidates', [])
    for candidate in candidates:
        # 提取 candidate 元数据 (包括 finish_reason)
        meta = process_candidate_metadata(candidate)
        state.update(meta)

        # 提取 content parts
        content = candidate.get('content', {})
        parts = content.get('parts', [])
        for part in parts:
            if path_index != -1:
                # 用列表收集，避免同一 path_index 的多个 part 互相覆盖（截断 bug）
                if path_index not in state['parts_by_path']:
                    state['parts_by_path'][path_index] = []
                state['parts_by_path'][path_index].append(part)
            else:
                state['unindexed_parts'].append(part)
