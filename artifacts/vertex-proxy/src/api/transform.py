"""请求和响应转换工具模块

负责：
1. 将 Gemini 格式的 Payload 转换为 Vertex AI 内部格式
2. 将流式响应聚合成完整的非流式响应
"""

import json
import time
from typing import Any, cast

from src.core.errors import (
    VertexError,
    InternalError,
    parse_error_response,
)
from src.api.model_config import ModelConfigBuilder
from src.utils.logger import get_logger
from src.utils.string_utils import snake_to_camel, camel_to_snake

logger = get_logger(__name__)

class RequestTransformer:
    """请求参数转换器"""
    
    def __init__(self, model_builder: ModelConfigBuilder):
        self.model_builder = model_builder

    def build_vertex_payload(
        self,
        model: str,
        gemini_payload: dict[str, Any],
        original_body: dict[str, Any],
        kwargs: dict[str, Any]
    ) -> dict[str, Any]:
        """
        构建 Vertex AI 请求 Payload
        
        Returns:
            new_body
        """
        original_vars: Any = original_body.get('variables', {})
        new_variables: dict[str, Any]
        if hasattr(original_vars, 'model_dump'):
             new_variables = cast(dict[str, Any], original_vars.model_dump())
        elif isinstance(original_vars, dict):
            new_variables = {str(k): v for k, v in cast(dict[Any, Any], original_vars).items()}
        else:
             new_variables = {}

        target_model = self.model_builder.parse_model_name(model)
        new_variables['model'] = target_model

        # 支持的字段列表（统一使用 camelCase 格式）
        supported_fields = [
            'contents', 'tools', 'toolConfig', 'systemInstruction',
            'safetySettings', 'generationConfig'
        ]
        
        # 处理 systemInstruction：如果没有 user content，则转换为 user message
        self._handle_system_instruction(new_variables)

        try:
            from src.core.types import GeminiPayload
            gemini_payload_obj = GeminiPayload.model_validate(gemini_payload)
            dumped_payload = gemini_payload_obj.model_dump(by_alias=True, exclude_none=True)
            
            for field in supported_fields:
                if field in dumped_payload:
                     new_variables[field] = dumped_payload[field]
        except Exception as e:
            logger.debug(f"Pydantic 验证失败，使用基础转换: {e}")
            # 尝试直接从 gemini_payload 透传字段，支持 snake_case 和 camelCase
            for field in supported_fields:
                # 优先使用 camelCase 版本
                if field in gemini_payload:
                    new_variables[field] = gemini_payload[field]
                else:
                    # 尝试 snake_case 版本
                    snake_field = camel_to_snake(field)
                    if snake_field in gemini_payload:
                        new_variables[field] = gemini_payload[snake_field]

        # 特殊处理：contents 格式转换
        if 'contents' in new_variables:
            converted_contents = self._handle_base64_in_contents(new_variables['contents'])
            # 过滤掉空的 parts（Vertex AI 要求每个 content 至少有一个 part）
            converted_contents = self._filter_empty_contents(converted_contents)
            # 处理 thoughtSignature 字段的 base64 编码
            converted_contents = self._handle_thought_signature(converted_contents)
            new_variables['contents'] = converted_contents
        
        # 特殊处理：tools 格式转换
        if 'tools' in new_variables:
            normalized_tools = self._normalize_tools_format(new_variables['tools'])
            if normalized_tools:
                new_variables['tools'] = normalized_tools
            else:
                # 如果转换结果为空列表，确保移除 tools 字段，避免 API 报错
                if 'tools' in new_variables:
                    del new_variables['tools']
        
        # 特殊处理：toolConfig 格式转换
        if 'toolConfig' in new_variables:
            new_variables['toolConfig'] = self._convert_tools_format(new_variables['toolConfig'])

        # 特殊处理 generationConfig (使用 ModelConfigBuilder 进行格式转换)
        gen_config = self.model_builder.build_generation_config(
            gen_config={},
            gemini_payload=gemini_payload,
            **kwargs
        )
        if gen_config:
            new_variables['generationConfig'] = gen_config
            
        # 特殊处理 safetySettings (如果未提供，则使用默认的宽松设置)
        if 'safetySettings' not in new_variables and 'safety_settings' not in gemini_payload:
            new_variables['safetySettings'] = self.model_builder.build_safety_settings()

        new_body: dict[str, Any] = {
            "querySignature": original_body.get('querySignature'),
            "operationName": original_body.get('operationName'),
            "variables": new_variables
        }
        
        return new_body

    def _convert_tools_format(self, data: Any) -> Any:
        """专门处理工具格式转换，统一转换为 camelCase"""
        if isinstance(data, dict):
            new_dict: dict[str, Any] = {}
            data_dict: dict[str, Any] = cast(dict[str, Any], data)
            for k, v in data_dict.items():
                # 转换 function_declarations 为 functionDeclarations
                if k in ['function_declarations', 'functionDeclarations']:
                    new_dict['functionDeclarations'] = self._convert_tools_format(v)
                elif k == "parametersJsonSchema" and isinstance(v, dict):
                    # parametersJsonSchema 需要特殊处理，确保 properties 和 required 字段一致
                    new_dict[k] = self._convert_parameters_schema(cast(dict[str, Any], v))
                elif k == "parameters" and isinstance(v, dict):
                    # Schema 对象需要特殊处理
                    converted_v = v.copy() if isinstance(v, dict) else v
                    new_dict[k] = self._to_native_schema(converted_v)
                elif k == "name" and not v:  # Vertex AI Function name cannot be empty
                    continue
                else:
                    # 对于其他字段，转换为 camelCase（除了特殊字段）
                    camel_key = snake_to_camel(k) if '_' in k else k
                    new_dict[camel_key] = self._convert_tools_format(v) if isinstance(v, (dict, list)) else v
            return new_dict
        elif isinstance(data, list):
            return [self._convert_tools_format(item) for item in cast(list[Any], data)]
        else:
            return data

    def _convert_parameters_schema(self, schema: dict[str, Any]) -> dict[str, Any]:
        """
        转换 parametersJsonSchema，确保 properties 和 required 字段中的参数名一致
        统一使用 snake_case 格式，避免 camelCase 和 snake_case 混用导致的不匹配
        """
        new_schema: dict[str, Any] = schema.copy()
        
        # 处理 properties 字段：将 camelCase 转换为 snake_case
        if 'properties' in new_schema and isinstance(new_schema['properties'], dict):
            old_properties: dict[str, Any] = cast(dict[str, Any], new_schema['properties'])
            new_properties: dict[str, Any] = {}
            
            for prop_name, prop_def in old_properties.items():
                # 将 camelCase 转换为 snake_case
                snake_name = camel_to_snake(str(prop_name))
                new_properties[snake_name] = prop_def
                
                # 递归处理嵌套的 schema
                if isinstance(prop_def, dict):
                    new_properties[snake_name] = self._convert_parameters_schema(cast(dict[str, Any], prop_def))
            
            new_schema['properties'] = new_properties
        
        # 处理 required 字段：确保使用 snake_case（通常已经是正确的）
        if 'required' in new_schema and isinstance(new_schema['required'], list):
            # required 字段通常已经是 snake_case，但为了保险起见，也进行转换
            new_required: list[Any] = []
            for req_name in cast(list[Any], new_schema['required']):
                if isinstance(req_name, str):
                    snake_name = camel_to_snake(req_name)
                    new_required.append(snake_name)
                else:
                    new_required.append(req_name)
            new_schema['required'] = new_required
        
        # 处理其他可能包含 schema 的字段
        for key, value in new_schema.items():
            if key not in ['properties', 'required'] and isinstance(value, dict):
                new_schema[key] = self._convert_parameters_schema(cast(dict[str, Any], value))
        
        return new_schema

    def _to_native_schema(self, standard_schema: dict[str, Any]) -> dict[str, Any]:
        """
        将标准 JSON Schema 转换为 Vertex AI 原生 Map-style Schema
        
        Args:
            standard_schema: 标准 JSON Schema 对象
            
        Returns:
            Vertex AI 原生 Schema
        """
        
        native_schema = standard_schema.copy()
        
        # Vertex AI 要求类型必须是大写 (例如: STRING, OBJECT, INTEGER)
        if 'type' in native_schema and isinstance(native_schema['type'], str):
            native_schema['type'] = native_schema['type'].upper()
            
        if 'properties' in native_schema and isinstance(native_schema['properties'], dict):
            native_props: list[dict[str, str | dict[str, Any]]] = []
            props_dict = cast(dict[str, Any], native_schema['properties'])
            for key, value in props_dict.items():
                # 递归处理嵌套对象
                if isinstance(value, dict):
                    converted_value = self._to_native_schema(cast(dict[str, Any], value))
                else:
                    converted_value = {}
                
                native_props.append({
                    "key": str(key),
                    "value": converted_value
                })
            native_schema['properties'] = native_props
        
        # 处理数组项
        if 'items' in native_schema and isinstance(native_schema['items'], dict):
            items_dict = cast(dict[str, Any], native_schema['items'])
            native_schema['items'] = self._to_native_schema(items_dict)
            
        return native_schema
    
    def _handle_system_instruction(self, new_variables: dict[str, Any]) -> None:
        """处理 systemInstruction：如果没有 user content，则转换为 user message"""
        system_instruction_content = new_variables.get('systemInstruction')
        if not system_instruction_content:
            return
            
        contents = new_variables.get('contents', [])
        
        # 检查是否已有 user 角色
        contents_list: list[Any] = cast(list[Any], contents) if isinstance(contents, list) else []
        has_user_role = any(
            isinstance(content, dict) and cast(dict[str, Any], content).get('role') == 'user'
            for content in contents_list
        )
        
        if has_user_role:
            return
            
        # 提取文本内容
        text_from_system = self._extract_text_from_instruction(system_instruction_content)
        if not text_from_system:
            return
            
        # 转换为 user message
        user_message = {
            'role': 'user',
            'parts': [{'text': text_from_system}]
        }
        
        # 显式转换 contents 为 list[Any] 以修复 pylance 报错
        new_contents: list[Any] = list(contents_list)
        new_contents.insert(0, user_message)
        new_variables['contents'] = new_contents
        del new_variables['systemInstruction']
    
    def _extract_text_from_instruction(self, instruction: Any) -> str:
        """从 system instruction 中提取文本内容"""
        if isinstance(instruction, str):
            return instruction
        elif isinstance(instruction, dict):
            instruction_dict = cast(dict[str, Any], instruction)
            parts = instruction_dict.get('parts', [])
            if isinstance(parts, list) and len(cast(list[Any], parts)) > 0:
                parts_list: list[Any] = cast(list[Any], parts)
                first_part: Any = parts_list[0]
                if isinstance(first_part, dict):
                    return str(cast(dict[str, Any], first_part).get('text', ''))
        return ""
    
    def _normalize_tools_format(self, tools: Any) -> list[dict[str, Any]]:
        """标准化 tools 格式为 Vertex AI 期望的格式 (List[Tool])"""
        converted_tools: Any = self._convert_tools_format(tools)
        
        if isinstance(converted_tools, dict):
            # 如果是字典，且包含 functionDeclarations，将其包裹在列表中
            if 'functionDeclarations' in converted_tools:
                return [cast(dict[str, Any], converted_tools)]
            # 如果是单个 FunctionDeclaration，包裹成 Tool 再包裹在列表中
            if 'name' in converted_tools:
                return [{"functionDeclarations": [cast(dict[str, Any], converted_tools)]}]
            return []
            
        if not isinstance(converted_tools, list) or len(cast(list[Any], converted_tools)) == 0:
            return []
            
        converted_tools_list: list[Any] = cast(list[Any], converted_tools)
        first_item: Any = converted_tools_list[0]
        if not isinstance(first_item, dict):
            return []
            
        # 情况 1: 列表元素是 FunctionDeclaration (没有 functionDeclarations 字段)
        if 'name' in first_item and 'functionDeclarations' not in first_item:
            return [{"functionDeclarations": cast(list[dict[str, Any]], converted_tools_list)}]
            
        # 情况 2: 列表元素是 Tool (包含 functionDeclarations)
        # Vertex AI 期望 tools 是一个列表，直接返回
        if 'functionDeclarations' in first_item:
            return cast(list[dict[str, Any]], converted_tools_list)
            
        return []

    def _handle_base64_in_contents(self, contents: Any) -> Any:
        """
        递归处理 contents 中的 base64 数据。
        将 URL-safe Base64 转换为标准 Base64 并补全 padding。
        """
        try:
            if isinstance(contents, list):
                res_list: list[Any] = [self._handle_base64_in_contents(item) for item in cast(list[Any], contents)]
                return cast(Any, res_list)
            if isinstance(contents, dict):
                new_dict: dict[str, Any] = {}
                for k, v in cast(dict[str, Any], contents).items():
                    if k == 'inlineData' and isinstance(v, dict):
                        v_dict = cast(dict[str, Any], v)
                        if 'data' in v_dict and isinstance(v_dict['data'], str):
                            try:
                                b64_data: str = v_dict['data']
                                b64_data = b64_data.replace('-', '+').replace('_', '/')
                                padding = len(b64_data) % 4
                                if padding:
                                    b64_data += '=' * (4 - padding)
                                
                                new_inline_data = v_dict.copy()
                                new_inline_data['data'] = b64_data
                                new_dict[k] = new_inline_data
                                continue
                            except Exception:
                                pass
                    new_dict[k] = self._handle_base64_in_contents(v)
                return cast(Any, new_dict)
            return contents
        except Exception as e:
            logger.warning(f"Base64 内容处理失败: {e}")
            return cast(Any, contents)

    def _filter_empty_contents(self, contents: Any) -> Any:
        """
        过滤掉空的 contents（parts 为空数组的消息）
        Vertex AI 要求每个 content 至少包含一个 part
        """
        if not isinstance(contents, list):
            return contents
        
        filtered_contents: list[Any] = []
        contents_list: list[Any] = cast(list[Any], contents)
        
        # 收集所有 functionCall 的名称，用于修复 functionResponse
        function_call_names: list[str] = []
        for content in contents_list:
            if isinstance(content, dict):
                content_dict = cast(dict[str, Any], content)
                parts = content_dict.get('parts', [])
                if isinstance(parts, list):
                    for part in cast(list[Any], parts):
                        if isinstance(part, dict):
                            part_dict = cast(dict[str, Any], part)
                            func_call = part_dict.get('functionCall')
                            if isinstance(func_call, dict):
                                func_call_dict = cast(dict[str, Any], func_call)
                                name = func_call_dict.get('name')
                                if name and isinstance(name, str):
                                    function_call_names.append(name)
        
        for content in contents_list:
            if isinstance(content, dict):
                content_dict: dict[str, Any] = cast(dict[str, Any], content)
                parts = content_dict.get('parts', [])
                # 只保留有 parts 且 parts 不为空的 content
                if isinstance(parts, list) and len(cast(list[Any], parts)) > 0:
                    parts_list: list[Any] = cast(list[Any], parts)
                    # 过滤并验证 parts 中的有效内容
                    valid_parts: list[Any] = []
                    for part in parts_list:
                        if isinstance(part, dict):
                            part_dict = cast(dict[str, Any], part)
                            
                            # 清理并修复 part
                            cleaned_part = self._clean_part_metadata(part_dict, function_call_names)
                            if cleaned_part:
                                valid_parts.append(cleaned_part)
                    
                    if valid_parts:
                        # 更新 content 的 parts
                        filtered_content = content_dict.copy()
                        filtered_content['parts'] = valid_parts
                        filtered_contents.append(filtered_content)
                    else:
                        logger.debug(f"过滤掉空的 content: role={content_dict.get('role', 'unknown')}")
                else:
                    logger.debug(f"过滤掉空的 content: role={content_dict.get('role', 'unknown')}")
            else:
                # 非 Dict 类型的 content，保留
                filtered_contents.append(content)
        
        return filtered_contents

    def _clean_part_metadata(self, part_dict: dict[str, Any], function_call_names: list[str]) -> dict[str, Any] | None:
        """
        清理 part 中的空元数据字段，修复无效的 functionResponse
        
        Args:
            part_dict: 原始 part 字典
            function_call_names: 可用的函数调用名称列表
            
        Returns:
            清理后的 part 字典，如果 part 无效则返回 None
        """
        cleaned_part: dict[str, Any] = {}
        has_valid_content = False
        
        # 处理文本内容
        if 'text' in part_dict:
            text_value = part_dict['text']
            if text_value and str(text_value).strip():
                cleaned_part['text'] = text_value
                has_valid_content = True
        
        # 处理思考标记
        if 'thought' in part_dict:
            cleaned_part['thought'] = part_dict['thought']
        
        # 处理思考签名 (thoughtSignature)
        if 'thoughtSignature' in part_dict:
            cleaned_part['thoughtSignature'] = part_dict['thoughtSignature']
        
        # 处理函数调用
        if 'functionCall' in part_dict:
            func_call = part_dict['functionCall']
            if isinstance(func_call, dict):
                func_call_dict = cast(dict[str, Any], func_call)
                if func_call_dict.get('name'):  # 只保留有名称的函数调用
                    cleaned_part['functionCall'] = func_call
                    has_valid_content = True
        
        # 处理函数响应
        if 'functionResponse' in part_dict:
            func_response = part_dict['functionResponse']
            if isinstance(func_response, dict):
                func_response_dict = cast(dict[str, Any], func_response)
                current_name = func_response_dict.get('name')
                
                # 如果 name 为空，尝试修复
                if not current_name and function_call_names:
                    inferred_name = function_call_names[-1]  # 使用最后一个 functionCall 的名称
                    logger.warning(f"修复空的 functionResponse.name，推断为: {inferred_name}")
                    
                    fixed_func_response = func_response_dict.copy()
                    fixed_func_response['name'] = inferred_name
                    cleaned_part['functionResponse'] = fixed_func_response
                    has_valid_content = True
                elif current_name:
                    # name 不为空，直接保留
                    cleaned_part['functionResponse'] = func_response
                    has_valid_content = True
                # 如果 name 为空且无法推断，则丢弃这个 functionResponse
        
        # 处理内联数据
        if 'inlineData' in part_dict:
            inline_data = part_dict['inlineData']
            if isinstance(inline_data, dict):
                inline_data_dict = cast(dict[str, Any], inline_data)
                # 只保留有实际数据的 inlineData
                if (inline_data_dict.get('data') and
                    str(inline_data_dict['data']).strip() and
                    inline_data_dict.get('mimeType') and
                    str(inline_data_dict['mimeType']).strip()):
                    cleaned_part['inlineData'] = inline_data
                    has_valid_content = True
        
        # 处理文件数据
        if 'fileData' in part_dict:
            file_data = part_dict['fileData']
            if isinstance(file_data, dict):
                file_data_dict = cast(dict[str, Any], file_data)
                # 只保留有实际数据的 fileData
                if (file_data_dict.get('fileUri') and
                    str(file_data_dict['fileUri']).strip() and
                    file_data_dict.get('mimeType') and
                    str(file_data_dict['mimeType']).strip()):
                    cleaned_part['fileData'] = file_data
                    has_valid_content = True
        
        # 只返回有有效内容的 part
        if has_valid_content:
            return cleaned_part
        else:
            logger.debug("过滤掉没有有效内容的 part")
            return None

    def _handle_thought_signature(self, contents: Any) -> Any:
        """
        处理 thoughtSignature 字段的 base64 编码
        确保 thoughtSignature 字段正确编码为 base64 字符串
        """
        import base64
        
        if isinstance(contents, list):
            return [self._handle_thought_signature(item) for item in cast(list[Any], contents)]
        if isinstance(contents, dict):
            new_dict: dict[str, Any] = {}
            contents_dict: dict[str, Any] = cast(dict[str, Any], contents)
            for k, v in contents_dict.items():
                if k == 'parts' and isinstance(v, list):
                    v_list: list[Any] = cast(list[Any], v)
                    # 处理 parts 数组中的每个 part
                    new_parts: list[Any] = []
                    for part in v_list:
                        if isinstance(part, dict):
                            new_part: dict[str, Any] = cast(dict[str, Any], part).copy()
                            # 检查是否有 thoughtSignature 字段
                            if 'thoughtSignature' in new_part:
                                signature_value = new_part['thoughtSignature']
                                if isinstance(signature_value, str):
                                    # 如果是特定的字符串，进行 base64 编码
                                    if signature_value == "skip_thought_signature_validator":
                                        encoded_bytes = base64.b64encode(signature_value.encode('utf-8'))
                                        new_part['thoughtSignature'] = encoded_bytes.decode('utf-8')
                                    # 如果已经是 base64 编码的字符串，保持不变
                                    # 其他情况也保持不变
                            new_parts.append(new_part)
                        else:
                            new_parts.append(part)
                    new_dict[k] = new_parts
                else:
                    new_dict[k] = self._handle_thought_signature(v) if isinstance(v, (dict, list)) else v
            return new_dict
        return contents

    @staticmethod
    def prepare_headers(creds: dict[str, Any]) -> dict[str, str]:
        """准备请求头"""
        # 提取原始头信息
        headers = RequestTransformer._extract_headers_from_creds(creds)
        
        # 设置必要的头信息
        headers['content-type'] = 'application/json'
        
        # 移除可能导致问题的头
        problematic_headers = [
            'content-length', 'Content-Length', 'host', 'Host',
            'connection', 'Connection', 'accept-encoding'
        ]
        for header in problematic_headers:
            headers.pop(header, None)
            
        return headers
    
    @staticmethod
    def _extract_headers_from_creds(creds: dict[str, Any]) -> dict[str, str]:
        """从凭证中提取头信息"""
        if hasattr(creds, 'model_dump') and hasattr(creds, 'headers'):
            headers_attr = getattr(creds, 'headers')
            if isinstance(headers_attr, dict):
                return cast(dict[str, str], headers_attr).copy()
        
        raw_headers = creds.get('headers')
        if isinstance(raw_headers, dict):
            return cast(dict[str, str], raw_headers).copy()
            
        return {}


class ResponseAggregator:
    """响应聚合器"""

    @staticmethod
    async def aggregate_stream(stream_generator: Any, _raw_image_response: bool = False) -> dict[str, Any]:
        """
        聚合流式响应为非流式对象
        """
        all_parts: list[dict[str, Any]] = []
        finish_reason = "STOP"
        safety_ratings: list[dict[str, Any]] = []
        citation_metadata: dict[str, Any] = {}
        grounding_metadata: dict[str, Any] = {}
        token_count: int | None = None
        avg_logprobs: float | None = None
        candidate_index = 0
        usage_metadata: dict[str, Any] = {}
        
        create_time: str | None = None
        model_version: str | None = None
        prompt_feedback: dict[str, Any] = {}
        response_id: str | None = None
        
        try:
            async for chunk_str in stream_generator:
                actual_json_str = chunk_str.strip()
                if actual_json_str.startswith("data: "):
                    actual_json_str = actual_json_str[6:]
                
                if not actual_json_str:
                    continue
                
                try:
                    chunk = json.loads(actual_json_str)
                    
                    # 检查 chunk 是否包含错误 (使用统一解析逻辑)
                    parsed_error = parse_error_response(chunk)
                    if parsed_error:
                        raise parsed_error
                    
                    # 提取顶层元数据
                    create_time = create_time or chunk.get('createTime')
                    model_version = model_version or chunk.get('modelVersion')
                    prompt_feedback = prompt_feedback or chunk.get('promptFeedback', {})
                    response_id = response_id or chunk.get('responseId')
                    usage_metadata = usage_metadata or chunk.get('usageMetadata', {})
                    
                    candidates = chunk.get('candidates', [])
                    if candidates:
                        candidate = candidates[0]
                        
                        # 提取 parts
                        content_obj = candidate.get('content', {})
                        parts = content_obj.get('parts', [])
                        if parts:
                            all_parts.extend(parts)
                        
                        # 提取 candidate 元数据
                        finish_reason = candidate.get('finishReason') or finish_reason
                        safety_ratings = candidate.get('safetyRatings') or safety_ratings
                        citation_metadata = candidate.get('citationMetadata') or citation_metadata
                        grounding_metadata = candidate.get('groundingMetadata') or grounding_metadata
                        if candidate.get('tokenCount') is not None:
                            token_count = candidate['tokenCount']
                        if candidate.get('avgLogprobs') is not None:
                            avg_logprobs = candidate['avgLogprobs']
                        if candidate.get('index') is not None:
                            candidate_index = candidate['index']
                            
                except json.JSONDecodeError as e:
                    logger.debug(f"JSON 解析失败，跳过此块: {e}")
                    continue
                    
        except VertexError:
            raise
        except Exception as e:
            raise InternalError(message=f"Non-streaming request error: {e}")
        
        # 处理图片响应特例
        full_text_content = "".join(str(p['text']) for p in all_parts if 'text' in p)

        if full_text_content.startswith("![Generated Image](data:"):
            start_idx = full_text_content.find('(') + 1
            end_idx = full_text_content.rfind(')')
            data_url = full_text_content[start_idx:end_idx]
            if _raw_image_response:
                try:
                    _, encoded = data_url.split(',', 1)
                    return {"created": int(time.time()), "data": [{"b64_json": encoded}]}
                except Exception:
                    return {"created": int(time.time()), "data": []}
            else:
                return {"resultUrl": data_url}
        
        # 构建最终响应
        if not all_parts:
            all_parts = [{"text": " "}]
        
        result_candidate: dict[str, Any] = {
            "content": {
                "parts": all_parts,
                "role": "model"
            },
            "finishReason": finish_reason.upper(),
            "index": candidate_index
        }
        
        # 构建候选结果，只添加非空字段
        optional_candidate_fields: dict[str, Any] = {
            "safetyRatings": safety_ratings,
            "citationMetadata": citation_metadata,
            "groundingMetadata": grounding_metadata,
            "tokenCount": token_count,
            "avgLogprobs": avg_logprobs
        }
        
        for key, value in optional_candidate_fields.items():
            if value is not None and value != [] and value != {}:
                result_candidate[key] = value
        
        # 构建最终结果，只添加非空字段
        result: dict[str, Any] = {"candidates": [result_candidate]}
        
        optional_result_fields: dict[str, Any] = {
            "createTime": create_time,
            "modelVersion": model_version,
            "promptFeedback": prompt_feedback,
            "responseId": response_id,
            "usageMetadata": usage_metadata
        }
        
        for key, value in optional_result_fields.items():
            if value is not None and value != {} and value != "":
                result[key] = value
        
        return result
