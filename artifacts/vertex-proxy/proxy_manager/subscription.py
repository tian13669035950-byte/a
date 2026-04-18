"""订阅链接解析模块，支持 VLESS / VMESS (base64列表) 和 Clash YAML 格式"""

import base64
import json
import ssl
import urllib.request
from typing import Any
from urllib.parse import urlparse, parse_qs, unquote


def _safe_b64decode(s: str) -> bytes:
    s = s.strip().replace("-", "+").replace("_", "/")
    pad = len(s) % 4
    if pad:
        s += "=" * (4 - pad)
    return base64.b64decode(s)


def parse_vless(url: str) -> dict[str, Any] | None:
    """解析 vless:// 链接"""
    try:
        parsed = urlparse(url)
        params = parse_qs(parsed.query)
        name = unquote(parsed.fragment) if parsed.fragment else parsed.hostname or "unknown"
        return {
            "protocol": "vless",
            "name": name,
            "uuid": parsed.username or "",
            "address": parsed.hostname or "",
            "port": parsed.port or 443,
            "network": params.get("type", ["tcp"])[0],
            "security": params.get("security", ["none"])[0],
            "sni": params.get("sni", params.get("host", [parsed.hostname or ""]))[0],
            "path": unquote(params.get("path", ["/"])[0]),
            "host": params.get("host", [parsed.hostname or ""])[0],
            "flow": params.get("flow", [""])[0],
            "fp": params.get("fp", [""])[0],
            "raw": url,
        }
    except Exception:
        return None


def parse_vmess(url: str) -> dict[str, Any] | None:
    """解析 vmess:// 链接"""
    try:
        encoded = url[8:]
        data = json.loads(_safe_b64decode(encoded).decode("utf-8"))
        return {
            "protocol": "vmess",
            "name": data.get("ps", "unknown"),
            "uuid": data.get("id", ""),
            "address": data.get("add", ""),
            "port": int(data.get("port", 443)),
            "network": data.get("net", "tcp"),
            "security": data.get("tls", "none"),
            "sni": data.get("sni", data.get("host", data.get("add", ""))),
            "path": data.get("path", "/"),
            "host": data.get("host", data.get("add", "")),
            "alter_id": int(data.get("aid", 0)),
            "raw": url,
        }
    except Exception:
        return None


def _parse_clash_proxy(p: dict[str, Any]) -> dict[str, Any] | None:
    """将 Clash 格式的代理对象转换为内部格式"""
    try:
        ptype = str(p.get("type", "")).lower()
        if ptype not in ("vless", "vmess"):
            return None

        network = str(p.get("network", "tcp")).lower()
        tls = p.get("tls", False)
        security = "tls" if tls else "none"

        # 从 ws-opts / grpc-opts 等提取 path/host
        ws_opts = p.get("ws-opts", p.get("ws-options", {})) or {}
        headers = ws_opts.get("headers", {}) or {}
        path = ws_opts.get("path", "/")
        host = headers.get("Host", headers.get("host", p.get("servername", p.get("server", ""))))

        node: dict[str, Any] = {
            "protocol": ptype,
            "name": str(p.get("name", "unknown")),
            "uuid": str(p.get("uuid", p.get("id", ""))),
            "address": str(p.get("server", "")),
            "port": int(p.get("port", 443)),
            "network": network,
            "security": security,
            "sni": str(p.get("servername", p.get("sni", p.get("server", "")))),
            "path": str(path),
            "host": str(host),
            "fp": str(p.get("client-fingerprint", p.get("fingerprint", "chrome"))),
            "skip_cert_verify": bool(p.get("skip-cert-verify", False)),
        }

        if ptype == "vmess":
            node["alter_id"] = int(p.get("alterId", p.get("alter_id", 0)))

        return node
    except Exception:
        return None


def _parse_clash_yaml(content: str) -> list[dict[str, Any]]:
    """解析 Clash YAML 订阅"""
    import yaml  # type: ignore
    try:
        data = yaml.safe_load(content)
    except Exception as e:
        raise RuntimeError(f"YAML 解析失败: {e}")

    if not isinstance(data, dict):
        return []

    raw_proxies = data.get("proxies", data.get("Proxy", []))
    if not raw_proxies:
        return []

    nodes: list[dict[str, Any]] = []
    for p in raw_proxies:
        if not isinstance(p, dict):
            continue
        node = _parse_clash_proxy(p)
        if node:
            nodes.append(node)
    return nodes


def fetch_and_parse(sub_url: str) -> list[dict[str, Any]]:
    """拉取订阅链接并解析所有节点，自动检测格式"""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    try:
        req = urllib.request.Request(
            sub_url,
            headers={"User-Agent": "ClashForWindows/0.20.0"}
        )
        with urllib.request.urlopen(req, timeout=20, context=ctx) as resp:
            raw = resp.read().decode("utf-8", errors="ignore").strip()
    except Exception as e:
        raise RuntimeError(f"拉取订阅失败: {e}")

    # 优先尝试 Clash YAML 格式检测
    is_clash = (
        raw.lstrip().startswith("proxies:") or
        "proxies:" in raw[:2000] or
        raw.lstrip().startswith("port:") or
        raw.lstrip().startswith("dns:")
    )

    if is_clash:
        return _parse_clash_yaml(raw)

    # 先检查原始内容是否直接包含 vless/vmess 链接（明文格式）
    raw_lines = raw.strip().splitlines()
    has_plain_links = any(
        l.strip().startswith("vless://") or l.strip().startswith("vmess://")
        for l in raw_lines[:50]
    )

    if has_plain_links:
        lines = raw_lines
    else:
        # 尝试 base64 解码
        try:
            decoded = _safe_b64decode(raw).decode("utf-8", errors="ignore")
            lines = decoded.strip().splitlines()
        except Exception:
            lines = raw_lines

    nodes: list[dict[str, Any]] = []
    for line in lines:
        line = line.strip()
        if line.startswith("vless://"):
            node = parse_vless(line)
            if node:
                nodes.append(node)
        elif line.startswith("vmess://"):
            node = parse_vmess(line)
            if node:
                nodes.append(node)

    if not nodes:
        preview = raw[:120].replace("\n", " ").replace("\r", "")
        raise RuntimeError(
            f"订阅内容解析出 0 个节点（共 {len(raw)} 字节）。"
            f"目前只支持 vless://、vmess:// 和 Clash YAML 格式。"
            f"内容预览：{preview!r}"
        )

    return nodes
