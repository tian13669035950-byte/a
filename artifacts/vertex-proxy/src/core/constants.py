"""
核心常量定义模块

包含API端口、配置文件路径等全局常量。
"""

from pathlib import Path
from .config import load_config

_config = load_config()
_ROOT_DIR = Path(__file__).parent.parent.parent

# API 服务端口
PORT_API = _config.get("port_api", 2156)

# 配置文件路径 (已移至 config/ 文件夹)
MODELS_CONFIG_FILE = str(_ROOT_DIR / "config" / "models.json")
STATS_FILE = str(_ROOT_DIR / "config" / "stats.json")
CONFIG_FILE = str(_ROOT_DIR / "config" / "config.json")
