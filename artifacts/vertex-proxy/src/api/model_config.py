"""模型配置构建器"""

import json
import time
from typing import Any, cast

from src.core import MODELS_CONFIG_FILE
from ..utils.logger import get_logger
from ..utils.string_utils import snake_to_camel

# 初始化日志
logger = get_logger(__name__)


class ModelConfigBuilder:
    """解析模型名称、处理后缀、构建生成配置"""
    
    _cached_map: dict[str, str] | None = None
    _last_load_time: float = 0
    
    def __init__(self) -> None:
        # 只有在启动阶段打印
        from src.utils.logger import get_request_id
        if not get_request_id():
            logger.info("模型配置构建器初始化完成", extra={
                "model_count": len(self._get_model_map())
            })
    
    def _get_model_map(self) -> dict[str, str]:
        # 简单缓存机制，每 60 秒检查一次文件更新
        current_time = time.time()
        if ModelConfigBuilder._cached_map is not None and current_time - ModelConfigBuilder._last_load_time < 60:
            return ModelConfigBuilder._cached_map
            
        try:
            with open(MODELS_CONFIG_FILE, 'r', encoding='utf-8') as f:
                config = json.load(f)
                ModelConfigBuilder._cached_map = cast(dict[str, str], config.get('alias_map', {}))
                ModelConfigBuilder._last_load_time = current_time
                logger.debug("模型配置文件加载成功", extra={
                    "config_file": MODELS_CONFIG_FILE,
                    "alias_count": len(ModelConfigBuilder._cached_map)
                })
        except Exception as e:
            logger.warning(f"模型配置文件加载失败，使用空配置", extra={
                "config_file": MODELS_CONFIG_FILE,
                "error": str(e)
            })
            if ModelConfigBuilder._cached_map is None:
                ModelConfigBuilder._cached_map = {}
        
        return ModelConfigBuilder._cached_map or {}
    
    def parse_model_name(self, model: str) -> str:
        """
        解析模型名称，返回 backend_model
        """
        return self._get_model_map().get(model, model)
    
    def build_generation_config(
        self,
        gen_config: dict[str, Any],
        gemini_payload: dict[str, Any] | None = None,
        **kwargs: Any
    ) -> dict[str, Any]:
        """构建生成配置"""
        # 防止修改原始配置对象
        final_config = gen_config.copy()
        
        # 1. 直接合并用户提供的配置
        if gemini_payload:
            user_gen_config_raw = gemini_payload.get('generationConfig', {}) or gemini_payload.get('generation_config', {})
            if user_gen_config_raw:
                user_gen_config: dict[str, Any] = {}
                # 显式转换为 Dict (如果它是 Pydantic model)
                if hasattr(user_gen_config_raw, 'model_dump'):
                     user_gen_config = user_gen_config_raw.model_dump(exclude_none=True)
                elif isinstance(user_gen_config_raw, dict):
                     user_gen_config = cast(dict[str, Any], user_gen_config_raw)
                
                if user_gen_config:
                    final_config.update(user_gen_config)

        # 1.5 合并 kwargs 中的生成配置参数
        for k, v in kwargs.items():
            # 直接添加所有 kwargs 参数，让转换函数处理驼峰转换
            final_config[k] = v

        # 2. 统一转换为 camelCase (适配 Vertex AI API)
        return self._convert_to_gemini_format(final_config)

    def _convert_to_gemini_format(self, config: dict[str, Any]) -> dict[str, Any]:
        """将 snake_case 配置转换为 camelCase"""
        converted: dict[str, Any] = {}
        for k, v in config.items():
            camel_key = snake_to_camel(k)
            
            # 特殊处理 thinkingConfig 中的 thinkingLevel 值
            if camel_key == "thinkingConfig" and isinstance(v, dict):
                thinking_config: dict[str, Any] = cast(dict[str, Any], v).copy()
                if "thinkingLevel" in thinking_config:
                    # 将小写的 thinking level 转换为大写
                    level = thinking_config["thinkingLevel"]
                    if isinstance(level, str):
                        thinking_config["thinkingLevel"] = level.upper()
                converted[camel_key] = thinking_config
            else:
                converted[camel_key] = v
                
        return converted
    
    def build_safety_settings(self) -> list[dict[str, str]]:
        """构建安全设置"""
        return [
            {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_CIVIC_INTEGRITY", "threshold": "BLOCK_NONE"}
        ]
