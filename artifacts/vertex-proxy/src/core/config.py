"""配置加载"""

import json
import os
from typing import Any, cast
from pathlib import Path
from src.utils.logger import get_logger
from .types import AppConfig

# 初始化日志
logger = get_logger(__name__)

CONFIG_FILE = str(Path(__file__).parent.parent.parent / "config" / "config.json")

def load_config() -> dict[str, Any]:
    """加载配置文件"""
    default_config = AppConfig()
    
    if not os.path.exists(CONFIG_FILE):
        logger.info("配置文件不存在，使用默认配置", extra={
            "config_file": CONFIG_FILE
        })
        return default_config.model_dump()
        
    try:
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            file_config = json.load(f)
            
            # 将默认配置转为字典
            config_dict = default_config.model_dump()
            
            # 更新其他配置
            config_dict.update(file_config)
            
            # 重新验证并创建模型实例
            final_config = AppConfig(**config_dict)
            final_dict = final_config.model_dump()
            
            # 只有在没有请求上下文时（即启动时）打印加载日志
            from src.utils.logger import get_request_id
            if not get_request_id():
                logger.info("配置文件加载成功", extra={
                    "config_file": CONFIG_FILE,
                    "port_api": final_dict.get("port_api"),
                    "debug_mode": final_dict.get("debug")
                })
            return final_dict
            
    except Exception as e:
        logger.error(f"配置文件加载失败，使用默认配置", extra={
            "config_file": CONFIG_FILE,
            "error": str(e)
        })
        return default_config.model_dump()
