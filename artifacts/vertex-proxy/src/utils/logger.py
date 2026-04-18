"""
Vertex AI Proxy 日志系统

优化后的日志系统：
- 简洁、直观的彩色输出
- 自动提取模块名和上下文
- 增强的调试支持 (支持字典/JSON 自动美化)
- 兼容标准 logging 接口
"""

import logging
from logging.handlers import RotatingFileHandler
import sys
import os
import json
import uuid
from datetime import datetime
from typing import Any
from contextvars import ContextVar

# ==================== 上下文变量 ====================
request_id_var: ContextVar[str] = ContextVar('request_id', default='')
# 用于存储当前请求的元数据
request_info_var: ContextVar[dict[str, Any]] = ContextVar('request_info', default={})

# ==================== ANSI 颜色代码 ====================
class Colors:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    
    RED = "\033[31m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    BLUE = "\033[34m"
    MAGENTA = "\033[35m"
    CYAN = "\033[36m"
    WHITE = "\033[37m"
    
    BRIGHT_RED = "\033[91m"
    BRIGHT_GREEN = "\033[92m"
    BRIGHT_YELLOW = "\033[93m"
    BRIGHT_BLUE = "\033[94m"
    BRIGHT_MAGENTA = "\033[95m"
    BRIGHT_CYAN = "\033[96m"
    BRIGHT_WHITE = "\033[97m"

# 自定义级别
SUCCESS_LEVEL = 25
logging.addLevelName(SUCCESS_LEVEL, "SUCCESS")

LEVEL_CONFIG = {
    logging.DEBUG: (Colors.DIM + Colors.CYAN, "🔍", "DEBUG"),
    logging.INFO: (Colors.CYAN, "ℹ️ ", "INFO"),
    SUCCESS_LEVEL: (Colors.BRIGHT_GREEN, "✅", "SUCCESS"),
    logging.WARNING: (Colors.BRIGHT_YELLOW, "⚠️ ", "WARN"),
    logging.ERROR: (Colors.BRIGHT_RED, "❌", "ERROR"),
    logging.CRITICAL: (Colors.BOLD + Colors.RED, "💀", "FATAL"),
}

# 模块缩写映射，保持对齐美观
MODULE_ABBR = {
    'vertex_client': 'Vertex',
    'error_logger': 'ErrLog',
    'diff_fixer': 'Diff',
    'processor': 'Stream',
}

class BetterFormatter(logging.Formatter):
    """更美观的格式化器"""
    
    def __init__(self, use_colors: bool = True):
        super().__init__()
        self.use_colors = use_colors and sys.stdout.isatty()

    def format(self, record: logging.LogRecord) -> str:
        # 1. 基本信息
        now = datetime.fromtimestamp(record.created).strftime('%H:%M:%S.%f')[:-3]
        level_tuple = LEVEL_CONFIG.get(record.levelno, (Colors.WHITE, "•", "LOG"))
        # 显式解包
        level_color = level_tuple[0]
        level_icon = level_tuple[1]
        level_name = level_tuple[2]
        
        # 2. 模块名处理
        module = record.name.split('.')[-1]
        module = MODULE_ABBR.get(module, module[:8].capitalize())
        
        # 3. 上下文信息 (Request ID)
        req_id = request_id_var.get()
        req_id_str = f" {Colors.DIM}|{Colors.RESET} {Colors.YELLOW}{req_id[:8]}{Colors.RESET}" if req_id else ""
        
        # 4. 消息处理
        message = record.getMessage()
        
        # 处理 debug_json 的 extra_data
        extra_data = getattr(record, 'extra_data', None)
        if extra_data is not None:
            try:
                # 保留原始消息（标签），然后附加格式化的JSON
                formatted_json = json.dumps(extra_data, indent=2, ensure_ascii=False, default=str)
                indented_json = "\n".join(f"    {line}" for line in formatted_json.splitlines())
                message += f"\n{indented_json}"
            except Exception as e:
                # 如果JSON序列化失败，附加错误信息
                message += f" (JSON序列化失败: {e})"
        # 处理直接传入的字典/列表消息
        elif isinstance(message, (dict, list)):
            try:
                formatted_json = json.dumps(message, indent=2, ensure_ascii=False, default=str)
                message = f"\n{formatted_json}"
                # 缩进
                message = "\n".join(f"    {line}" for line in message.splitlines())
            except Exception:
                # 失败则保持原始消息
                message = str(message)
        

        # 5. 异常处理
        exc_text = ""
        if record.exc_info:
            exc_text = "\n" + self.formatException(record.exc_info)
            # 缩进异常信息
            exc_text = "\n".join(f"    {line}" for line in exc_text.splitlines())

        if self.use_colors:
            return (
                f"{Colors.DIM}{now}{Colors.RESET} "
                f"{level_color}{level_icon} {level_name:<7}{Colors.RESET} "
                f"{Colors.MAGENTA}[{module:^10}]{Colors.RESET}"
                f"{req_id_str} "
                f"{message}{exc_text}"
            )
        else:
            return f"{now} {level_name:<7} [{module:^10}] {req_id[:8] if req_id else ''} {message}{exc_text}"

class _NoiseFilter(logging.Filter):
    """屏蔽 UI 高频轮询接口的访问日志，避免刷屏"""
    NOISY_SUBSTRINGS = (
        "/proxy-manager/status",
        "/proxy-manager/logs",
        "/proxy-manager/ip-check",
    )

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            return True
        return not any(p in msg for p in self.NOISY_SUBSTRINGS)


class LoggerManager:
    _instance = None
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized: return
        self._initialized = True
        self._log_level = logging.INFO
        self._setup_root_logger()

    def _setup_root_logger(self):
        root = logging.getLogger()
        root.setLevel(logging.DEBUG)
        root.handlers.clear()

        # Console Handler
        console = logging.StreamHandler(sys.stdout)
        console.setFormatter(BetterFormatter())
        console.setLevel(self._log_level)
        console.addFilter(_NoiseFilter())
        root.addHandler(console)

        # 屏蔽噪音
        for logger_name in ['httpx', 'httpcore', 'uvicorn', 'fastapi', 'hpack', 'h2', 'uvicorn.error', 'uvicorn.access']:
            l = logging.getLogger(logger_name)
            l.setLevel(logging.WARNING)
            l.propagate = True # 确保 uvicorn 的日志能传递到 root logger

    def configure(self, debug: bool = False, log_file: str | None = None):
        self._log_level = logging.DEBUG if debug else logging.INFO
        root = logging.getLogger()
        for h in root.handlers:
            h.setLevel(self._log_level)
            
        if log_file:
            os.makedirs(os.path.dirname(log_file), exist_ok=True)
            # 使用 'w' 模式在每次启动时清空日志文件
            file_h = RotatingFileHandler(log_file, maxBytes=2*1024*1024, backupCount=3, encoding='utf-8', delay=False)
            file_h.setFormatter(BetterFormatter(use_colors=False))
            file_h.setLevel(self._log_level)
            # 立即刷新，确保日志写入
            file_h.flush()
            root.addHandler(file_h)

class LoggerAdapter(logging.LoggerAdapter):  # type: ignore[type-arg]
    """增加便捷方法的 Logger 适配器"""
    def success(self, msg: object, *args: object, **kwargs: Any) -> None:
        self.log(SUCCESS_LEVEL, msg, *args, **kwargs)
        
    def debug_json(self, label: str, data: Any) -> None:
        """专门用于调试 JSON 数据"""
        if getattr(self.logger, "isEnabledFor", lambda x: False)(logging.DEBUG):
            try:
                # 只有在 debug 模式下才进行复杂的序列化操作
                formatted_data = json.loads(json.dumps(data, default=str)) if not isinstance(data, (dict, list)) else data
                self.logger._log(logging.DEBUG, f"{label}:", (), extra={'extra_data': formatted_data})
            except Exception as e:
                self.logger._log(logging.DEBUG, f"{label} (JSON解析失败: {e})", ())

    def debug_large(self, label: str, data: str) -> None:
        """专门用于调试大文本数据"""
        if getattr(self.logger, "isEnabledFor", lambda x: False)(logging.DEBUG):
            self.logger.debug(f"{label}:\n{data}")

def get_logger(name: str) -> LoggerAdapter:
    logger = logging.getLogger(name)
    return LoggerAdapter(logger, {})

# 全局便捷实例
manager = LoggerManager()

def configure_logging(debug: bool = False, log_dir: str = "logs"):
    log_path = os.path.join(log_dir, "app.log") if log_dir else None
    manager.configure(debug=debug, log_file=log_path)

def set_request_id(request_id: str | None = None):
    rid = request_id or uuid.uuid4().hex[:12]
    request_id_var.set(rid)
    return rid

def get_request_id() -> str:
    return request_id_var.get()

def clear_context():
    request_id_var.set('')
    request_info_var.set({})
