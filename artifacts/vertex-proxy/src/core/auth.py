"""API密钥认证模块 - 简化版"""

import os
import threading
from typing import Any
from pathlib import Path
from ..utils.logger import get_logger

# 初始化日志
logger = get_logger(__name__)

class APIKeyManager:
    """简化的API密钥管理器 - 只保留基本验证功能"""

    def __init__(self, keys_file: str | None = None):
        logger.info("初始化 API 密钥管理器")
        
        self.keys_file: str = keys_file or str(Path(__file__).parent.parent.parent / "config" / "api_keys.txt")
        self.api_keys: set[str] = set()
        self.key_names: dict[str, str] = {}  # api_key -> name
        self._lock: threading.Lock = threading.Lock()
        
        logger.debug(f"API 密钥文件路径: {self.keys_file}")

    def load_keys(self) -> bool:
        """从配置文件加载API密钥"""
        logger.info("开始加载 API 密钥")
        
        try:
            if not os.path.exists(self.keys_file):
                logger.warning(f"API 密钥文件不存在: {self.keys_file}")
                return False

            with self._lock:
                self.api_keys.clear()
                self.key_names.clear()
                
                valid_count = 0
                error_count = 0

                logger.debug(f"读取密钥文件: {self.keys_file}")
                with open(self.keys_file, 'r', encoding='utf-8') as f:
                    for line_num, line in enumerate(f, 1):
                        line = line.strip()

                        # 跳过空行和注释行
                        if not line or line.startswith('#'):
                            continue

                        # 解析格式: key_name:api_key:description
                        parts = line.split(':', 2)
                        if len(parts) < 2:
                            logger.warning(f"第 {line_num} 行格式错误，跳过")
                            error_count += 1
                            continue

                        key_name = parts[0].strip()
                        api_key = parts[1].strip()

                        # 验证密钥格式
                        if not api_key.startswith('sk-'):
                            logger.warning(f"第 {line_num} 行密钥格式无效 ({key_name})，跳过")
                            error_count += 1
                            continue

                        self.api_keys.add(api_key)
                        self.key_names[api_key] = key_name
                        valid_count += 1
                        logger.debug(f"加载密钥: {key_name} ({api_key[:8]}...)")

            # 支持通过环境变量 API_KEY 注入密钥（生产环境强烈推荐）
            env_key = os.environ.get("API_KEY", "").strip()
            if env_key:
                if not env_key.startswith("sk-"):
                    env_key = "sk-" + env_key
                with self._lock:
                    self.api_keys.add(env_key)
                    self.key_names[env_key] = "env:API_KEY"
                logger.success(f"已从环境变量加载 API 密钥 ({env_key[:8]}...)")
                valid_count += 1

            # 默认密钥告警
            if "sk-123456" in self.api_keys and not env_key:
                logger.warning("=" * 60)
                logger.warning("正在使用默认 API 密钥 sk-123456，公网访问极不安全")
                logger.warning("请在 Replit Secrets 中设置 API_KEY 来覆盖")
                logger.warning("=" * 60)

            if valid_count > 0:
                logger.success(f"成功加载 {valid_count} 个 API 密钥")
            if error_count > 0:
                logger.warning(f"跳过 {error_count} 个无效条目")
                
            return True

        except Exception as e:
            logger.error(f"加载 API 密钥失败: {e}")
            return False

    def validate_key(self, api_key: str) -> bool:
        """验证API密钥是否有效"""
        if not api_key:
            logger.debug("API 密钥为空")
            return False

        is_valid = api_key.strip() in self.api_keys
        if is_valid:
            key_name = self.key_names.get(api_key.strip(), 'unknown')
            logger.debug(f"API 密钥验证成功: {key_name} ({api_key[:8]}...)")
        else:
            logger.debug(f"API 密钥验证失败: {api_key[:8]}...")
            
        return is_valid


# 全局密钥管理器实例
api_key_manager = APIKeyManager()
