"""Xray 进程管理 + 配置生成"""

import json
import os
import subprocess
import zipfile
import urllib.request
import ssl
import time
from typing import Any

PROJ_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
XRAY_BIN = os.path.join(PROJ_ROOT, "bin", "xray")
XRAY_CONFIG = "/tmp/xray_config.json"
LOG_FILE = "/tmp/proxy.log"

_xray_proc: subprocess.Popen | None = None


def _log(msg: str):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}\n"
    print(line, end="")
    try:
        with open(LOG_FILE, "a") as f:
            f.write(line)
    except Exception:
        pass


def ensure_xray() -> bool:
    """确保 xray 二进制存在，不存在则下载"""
    if os.path.exists(XRAY_BIN) and os.access(XRAY_BIN, os.X_OK):
        _log(f"xray 已存在: {XRAY_BIN}")
        return True

    os.makedirs(os.path.dirname(XRAY_BIN), exist_ok=True)
    _log("正在下载 xray-core...")

    urls = [
        "https://github.com/XTLS/Xray-core/releases/download/v25.3.6/Xray-linux-64.zip",
        "https://github.com/XTLS/Xray-core/releases/latest/download/Xray-linux-64.zip",
    ]
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    for url in urls:
        try:
            _log(f"下载地址: {url}")
            zip_path = "/tmp/xray.zip"
            urllib.request.urlretrieve(url, zip_path)
            with zipfile.ZipFile(zip_path, "r") as z:
                z.extract("xray", os.path.dirname(XRAY_BIN))
            os.chmod(XRAY_BIN, 0o755)
            _log("xray 下载成功")
            return True
        except Exception as e:
            _log(f"下载失败 ({url}): {e}")

    _log("xray 下载失败，所有地址均不可用")
    return False



def _wait_socks5_ready(host: str = "127.0.0.1", port: int = 1080, timeout: float = 5.0) -> bool:
    """轮询等待 socks5 端口可连接，最多等 timeout 秒"""
    import socket as _sock
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with _sock.create_connection((host, port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.1)
    return False


def _build_vless_outbound(node: dict[str, Any]) -> dict[str, Any]:
    stream: dict[str, Any] = {"network": node.get("network", "tcp")}
    security = node.get("security", "none")
    
    if security == "tls":
        stream["security"] = "tls"
        stream["tlsSettings"] = {
            "serverName": node.get("sni") or node.get("address", ""),
            "allowInsecure": False,
            "fingerprint": node.get("fp", "chrome") or "chrome",
        }
    elif security == "reality":
        stream["security"] = "reality"
        stream["realitySettings"] = {
            "serverName": node.get("sni") or node.get("address", ""),
            "fingerprint": node.get("fp", "chrome") or "chrome",
            "publicKey": node.get("pbk", ""),
            "shortId": node.get("sid", ""),
        }
    else:
        stream["security"] = "none"

    net = node.get("network", "tcp")
    if net == "ws":
        stream["wsSettings"] = {
            "path": node.get("path", "/"),
            "headers": {"Host": node.get("host") or node.get("address", "")},
        }
    elif net == "grpc":
        stream["grpcSettings"] = {"serviceName": node.get("path", "").lstrip("/")}
    elif net == "h2":
        stream["httpSettings"] = {
            "path": node.get("path", "/"),
            "host": [node.get("host") or node.get("address", "")],
        }

    user: dict[str, Any] = {"id": node["uuid"], "encryption": "none"}
    if node.get("flow"):
        user["flow"] = node["flow"]

    return {
        "protocol": "vless",
        "settings": {
            "vnext": [{"address": node["address"], "port": node["port"], "users": [user]}]
        },
        "streamSettings": stream,
    }


def _build_vmess_outbound(node: dict[str, Any]) -> dict[str, Any]:
    stream: dict[str, Any] = {"network": node.get("network", "tcp")}
    security = node.get("security", "none")

    if security == "tls":
        stream["security"] = "tls"
        stream["tlsSettings"] = {
            "serverName": node.get("sni") or node.get("address", ""),
            "allowInsecure": False,
        }
    else:
        stream["security"] = "none"

    net = node.get("network", "tcp")
    if net == "ws":
        stream["wsSettings"] = {
            "path": node.get("path", "/"),
            "headers": {"Host": node.get("host") or node.get("address", "")},
        }

    return {
        "protocol": "vmess",
        "settings": {
            "vnext": [{
                "address": node["address"],
                "port": node["port"],
                "users": [{"id": node["uuid"], "alterId": node.get("alter_id", 0), "security": "auto"}],
            }]
        },
        "streamSettings": stream,
    }


def _standard_inbound() -> dict[str, Any]:
    return {
        "tag": "socks-in",
        "port": 1080,
        "protocol": "socks",
        "listen": "127.0.0.1",
        "settings": {"auth": "noauth", "udp": True},
    }


def _standard_log() -> dict[str, Any]:
    return {"loglevel": "warning", "access": "/tmp/xray_access.log", "error": "/tmp/xray_error.log"}


def build_xray_config(node: dict[str, Any]) -> dict[str, Any]:
    proto = node.get("protocol", "vless")
    if proto == "vless":
        outbound = _build_vless_outbound(node)
    elif proto == "vmess":
        outbound = _build_vmess_outbound(node)
    else:
        raise ValueError(f"不支持的协议: {proto}")

    return {
        "log": _standard_log(),
        "inbounds": [_standard_inbound()],
        "outbounds": [outbound, {"tag": "direct", "protocol": "freedom"}],
    }


def build_xray_config_from_outbounds(outbounds: list[dict[str, Any]]) -> dict[str, Any]:
    """从自定义 outbounds 列表构建完整 xray 配置（保留分片等高级特性）"""
    # 确保有 direct 和 block outbound
    tags = {o.get("tag") for o in outbounds}
    if "direct" not in tags:
        outbounds = list(outbounds) + [{"tag": "direct", "protocol": "freedom"}]
    if "block" not in tags:
        outbounds = list(outbounds) + [{"tag": "block", "protocol": "blackhole"}]
    return {
        "log": _standard_log(),
        "inbounds": [_standard_inbound()],
        "outbounds": outbounds,
    }


def start_xray_from_outbounds(outbounds: list[dict[str, Any]]) -> tuple[bool, str]:
    """使用自定义 outbounds 启动 xray（支持 TLS 分片等高级配置）"""
    global _xray_proc
    if not ensure_xray():
        return False, "xray 二进制不存在且下载失败"
    stop_xray()
    try:
        config = build_xray_config_from_outbounds(outbounds)
        with open(XRAY_CONFIG, "w") as f:
            json.dump(config, f, indent=2, ensure_ascii=False)
        _log(f"xray 自定义配置已写入: {XRAY_CONFIG}")
    except Exception as e:
        return False, f"生成配置失败: {e}"
    try:
        _xray_proc = subprocess.Popen(
            [XRAY_BIN, "run", "-c", XRAY_CONFIG],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        time.sleep(0.3)
        if _xray_proc.poll() is not None:
            out, _ = _xray_proc.communicate()
            _log(f"xray 启动失败: {out}")
            return False, f"xray 进程退出: {out[:500]}"
        if not _wait_socks5_ready():
            _log("⚠️ xray 进程已起但 socks5 端口 5s 内未就绪")
        _log(f"xray 启动成功 (自定义配置), PID={_xray_proc.pid}")
        return True, ""
    except Exception as e:
        _log(f"启动 xray 异常: {e}")
        return False, str(e)


def start_xray(node: dict[str, Any]) -> tuple[bool, str]:
    """启动 xray 进程，成功返回 (True, '') 失败返回 (False, error_msg)"""
    global _xray_proc

    if not ensure_xray():
        return False, "xray 二进制不存在且下载失败"

    stop_xray()

    try:
        config = build_xray_config(node)
        with open(XRAY_CONFIG, "w") as f:
            json.dump(config, f, indent=2, ensure_ascii=False)
        _log(f"xray 配置已写入: {XRAY_CONFIG}")
    except Exception as e:
        return False, f"生成配置失败: {e}"

    try:
        _xray_proc = subprocess.Popen(
            [XRAY_BIN, "run", "-c", XRAY_CONFIG],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        time.sleep(0.3)
        if _xray_proc.poll() is not None:
            out, _ = _xray_proc.communicate()
            _log(f"xray 启动失败: {out}")
            return False, f"xray 进程退出: {out[:500]}"
        if not _wait_socks5_ready():
            _log("⚠️ xray 进程已起但 socks5 端口 5s 内未就绪")
        _log(f"xray 启动成功, PID={_xray_proc.pid}")
        return True, ""
    except Exception as e:
        _log(f"启动 xray 异常: {e}")
        return False, str(e)


def stop_xray():
    global _xray_proc
    try:
        subprocess.run(["pkill", "-f", "xray run"], capture_output=True)
    except Exception:
        pass
    if _xray_proc:
        try:
            _xray_proc.terminate()
            _xray_proc.wait(timeout=3)
        except Exception:
            try:
                _xray_proc.kill()
            except Exception:
                pass
        _xray_proc = None


def is_running() -> bool:
    if _xray_proc is None:
        return False
    return _xray_proc.poll() is None


def get_logs(lines: int = 80) -> str:
    """读取日志文件最后 N 行"""
    try:
        xray_err = ""
        if os.path.exists("/tmp/xray_error.log"):
            with open("/tmp/xray_error.log") as f:
                xray_err = f.read()
        main_log = ""
        if os.path.exists(LOG_FILE):
            with open(LOG_FILE) as f:
                all_lines = f.readlines()
                main_log = "".join(all_lines[-lines:])
        return main_log + ("\n--- xray error ---\n" + xray_err[-2000:] if xray_err else "")
    except Exception as e:
        return f"读取日志失败: {e}"
