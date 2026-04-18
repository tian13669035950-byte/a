"""管理后台

提供：
1. 自动生成管理员密码（首次启动写入 config.json）
2. cookie / Bearer token 会话
3. 服务设置（端口、debug、max_retries、密码修改）
4. API 密钥三段式 CRUD（name:key:description）

不包含订阅 / 节点选择 — 那些由 proxy_manager 处理（链接到 /proxy-manager）。
"""

import asyncio
import json
import os
import secrets
import time
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel

from src.core.auth import api_key_manager
from src.core.config import load_config
from src.utils.logger import get_logger

logger = get_logger(__name__)

# ==================== 路径 ====================

_ROOT_DIR = Path(__file__).parent.parent.parent
CONFIG_FILE = _ROOT_DIR / "config" / "config.json"
API_KEYS_FILE = _ROOT_DIR / "config" / "api_keys.txt"
STATIC_DIR = _ROOT_DIR / "static"

# ==================== 会话 ====================

_sessions: dict[str, float] = {}
SESSION_TTL = 7 * 24 * 3600  # 7 天


def _read_json(path: Path, default: Any) -> Any:
    try:
        if not path.exists():
            return default if not isinstance(default, dict) else dict(default)
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"读取 {path} 失败: {e}")
        return default if not isinstance(default, dict) else dict(default)


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


DEFAULT_ADMIN_PASSWORD = "1966197012"


def _get_admin_password() -> str:
    env_pw = os.environ.get("ADMIN_PASSWORD", "").strip()
    if env_pw:
        return env_pw
    cfg = _read_json(CONFIG_FILE, {})
    cfg_pw = str(cfg.get("admin_password") or "").strip()
    return cfg_pw or DEFAULT_ADMIN_PASSWORD


def ensure_admin_password() -> str:
    """启动时确保有管理员密码：环境变量 > 配置文件 > 内置默认"""
    env_pw = os.environ.get("ADMIN_PASSWORD", "").strip()
    if env_pw:
        logger.info("[Admin] 使用环境变量 ADMIN_PASSWORD 作为管理员密码")
        return env_pw

    cfg = _read_json(CONFIG_FILE, {})
    existing = str(cfg.get("admin_password") or "").strip()
    if existing:
        return existing

    cfg["admin_password"] = DEFAULT_ADMIN_PASSWORD
    _write_json(CONFIG_FILE, cfg)
    logger.info(f"[Admin] 使用内置默认密码：{DEFAULT_ADMIN_PASSWORD}（建议通过 ADMIN_PASSWORD 环境变量覆盖）")
    return DEFAULT_ADMIN_PASSWORD


def _issue_token() -> str:
    tok = secrets.token_urlsafe(32)
    _sessions[tok] = time.time() + SESSION_TTL
    return tok


def _check_token(token: Optional[str]) -> bool:
    if not token:
        return False
    exp = _sessions.get(token)
    if not exp:
        return False
    if exp < time.time():
        _sessions.pop(token, None)
        return False
    return True


def _require_auth(request: Request) -> None:
    token = None
    auth = request.headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        token = auth[7:].strip()
    if not token:
        token = request.cookies.get("admin_token")
    if not _check_token(token):
        raise HTTPException(status_code=401, detail="未登录或会话已过期")


# ==================== API 密钥文件 IO ====================

def _read_api_keys() -> list[dict[str, str]]:
    if not API_KEYS_FILE.exists():
        return []
    out: list[dict[str, str]] = []
    with open(API_KEYS_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split(":", 2)
            if len(parts) < 2:
                continue
            out.append({
                "name": parts[0].strip(),
                "key": parts[1].strip(),
                "description": parts[2].strip() if len(parts) >= 3 else "",
            })
    return out


def _write_api_keys(keys: list[dict[str, str]]) -> None:
    API_KEYS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = API_KEYS_FILE.with_suffix(API_KEYS_FILE.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("# 格式: name:key:description （由管理面板维护）\n")
        for k in keys:
            name = (k.get("name") or "").strip()
            key = (k.get("key") or "").strip()
            desc = (k.get("description") or "").strip()
            if not name or not key:
                continue
            if desc:
                f.write(f"{name}:{key}:{desc}\n")
            else:
                f.write(f"{name}:{key}\n")
    os.replace(tmp, API_KEYS_FILE)


# ==================== Pydantic Models ====================

class LoginBody(BaseModel):
    password: str


class SettingsBody(BaseModel):
    port_api: Optional[int] = None
    debug: Optional[bool] = None
    max_retries: Optional[int] = None
    admin_password: Optional[str] = None


class KeyBody(BaseModel):
    name: str
    key: str
    description: str = ""


# ==================== Router ====================

router = APIRouter()


@router.get("/admin")
async def admin_page() -> FileResponse:
    index = STATIC_DIR / "admin.html"
    if not index.exists():
        raise HTTPException(status_code=500, detail="admin.html 不存在")
    return FileResponse(str(index), media_type="text/html; charset=utf-8")


@router.post("/admin/api/login")
async def admin_login(body: LoginBody) -> dict[str, Any]:
    expected = _get_admin_password()
    if not expected:
        raise HTTPException(status_code=500, detail="管理员密码未初始化")
    if body.password != expected:
        await asyncio.sleep(0.5)  # 轻微延迟，防爆破
        raise HTTPException(status_code=401, detail="密码错误")
    tok = _issue_token()
    return {"token": tok, "ttl_seconds": SESSION_TTL}


@router.post("/admin/api/logout")
async def admin_logout(request: Request) -> dict[str, str]:
    auth = request.headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        _sessions.pop(auth[7:].strip(), None)
    return {"status": "ok"}


@router.get("/admin/api/settings")
async def get_settings(request: Request) -> dict[str, Any]:
    _require_auth(request)
    cfg = load_config()
    return {
        "port_api": cfg.get("port_api", 2156),
        "debug": bool(cfg.get("debug", False)),
        "max_retries": int(cfg.get("max_retries", 2)),
        "admin_password_env_locked": bool(os.environ.get("ADMIN_PASSWORD", "").strip()),
    }


@router.put("/admin/api/settings")
async def update_settings(body: SettingsBody, request: Request) -> dict[str, Any]:
    _require_auth(request)
    cfg = _read_json(CONFIG_FILE, {})
    notes: list[str] = []

    if body.port_api is not None:
        if not (1 <= body.port_api <= 65535):
            raise HTTPException(status_code=400, detail="端口必须在 1-65535")
        if cfg.get("port_api") != body.port_api:
            notes.append("端口变更需要重启服务才能生效")
        cfg["port_api"] = body.port_api

    if body.debug is not None:
        if cfg.get("debug") != bool(body.debug):
            notes.append("debug 模式变更需要重启服务才能完全生效")
        cfg["debug"] = bool(body.debug)

    if body.max_retries is not None:
        if body.max_retries < 0 or body.max_retries > 100:
            raise HTTPException(status_code=400, detail="max_retries 应在 0-100")
        cfg["max_retries"] = int(body.max_retries)

    if body.admin_password is not None:
        if os.environ.get("ADMIN_PASSWORD", "").strip():
            raise HTTPException(status_code=400, detail="当前由环境变量 ADMIN_PASSWORD 锁定，无法在面板修改")
        new_pw = body.admin_password.strip()
        if len(new_pw) < 6:
            raise HTTPException(status_code=400, detail="密码至少 6 位")
        cfg["admin_password"] = new_pw
        notes.append("管理员密码已更新，下次登录生效")

    _write_json(CONFIG_FILE, cfg)
    return {"status": "ok", "notes": notes}


@router.get("/admin/api/keys")
async def get_keys(request: Request) -> dict[str, Any]:
    _require_auth(request)
    return {"keys": _read_api_keys()}


@router.post("/admin/api/keys")
async def add_key(body: KeyBody, request: Request) -> dict[str, str]:
    _require_auth(request)
    name = body.name.strip()
    key = body.key.strip()
    if not name or not key:
        raise HTTPException(status_code=400, detail="name / key 不能为空")
    if ":" in name:
        raise HTTPException(status_code=400, detail="name 不能包含冒号")
    if not key.startswith("sk-"):
        raise HTTPException(status_code=400, detail="key 必须以 sk- 开头")

    keys = _read_api_keys()
    keys = [k for k in keys if k["name"] != name]  # 同名覆盖
    keys.append({"name": name, "key": key, "description": body.description or ""})
    _write_api_keys(keys)
    api_key_manager.load_keys()  # 热加载
    return {"status": "ok"}


@router.delete("/admin/api/keys/{name}")
async def delete_key(name: str, request: Request) -> dict[str, str]:
    _require_auth(request)
    keys = _read_api_keys()
    new_keys = [k for k in keys if k["name"] != name]
    if len(new_keys) == len(keys):
        raise HTTPException(status_code=404, detail="未找到该密钥")
    _write_api_keys(new_keys)
    api_key_manager.load_keys()
    return {"status": "ok"}
