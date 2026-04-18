"""代理管理器 FastAPI 路由 + 内嵌 HTML 前端"""

import json
import os
import time
import threading
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

from .subscription import fetch_and_parse, parse_vless, parse_vmess
from .xray_manager import start_xray, start_xray_from_outbounds, stop_xray, is_running, get_logs
from . import proxy_state
from .country_detect import detect_all, sort_nodes_by_priority, DEFAULT_COUNTRY_PRIORITY, COUNTRY_NAMES

router = APIRouter(prefix="/proxy-manager", tags=["proxy-manager"])

SUB_URL = "https://tian110110.us.ci/sub?token=e2fb1e6322ce2a3d02e0d28de5846ea6"
_cached_nodes: list = []
_detect_status: dict = {"running": False, "done": 0, "total": 0, "last_run": ""}

PROJ_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CUSTOM_NODES_FILE = os.path.join(PROJ_ROOT, "config", "custom_nodes.json")
ACTIVE_NODE_FILE = os.path.join(PROJ_ROOT, "config", "active_node.json")
CACHED_NODES_FILE = os.path.join(PROJ_ROOT, "config", "cached_nodes.json")
SETTINGS_FILE = os.path.join(PROJ_ROOT, "config", "settings.json")


# ── 设置（国家优先级）─────────────────────────────────────────────────────────

def _load_settings() -> dict:
    try:
        if os.path.exists(SETTINGS_FILE):
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return {"country_priority": DEFAULT_COUNTRY_PRIORITY}


def _save_settings(data: dict):
    os.makedirs(os.path.dirname(SETTINGS_FILE), exist_ok=True)
    with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def _get_priority() -> list[str]:
    return _load_settings().get("country_priority", DEFAULT_COUNTRY_PRIORITY)


# ── 活跃节点持久化（服务重启后自动恢复）────────────────────────────────────────

def _save_active_node(record: dict):
    """保存当前选中的节点信息到磁盘，供重启后恢复。"""
    os.makedirs(os.path.dirname(ACTIVE_NODE_FILE), exist_ok=True)
    with open(ACTIVE_NODE_FILE, "w", encoding="utf-8") as f:
        json.dump(record, f, indent=2, ensure_ascii=False)


def _save_nodes_to_disk(nodes: list):
    """把订阅节点列表（去掉 raw 字段）写入磁盘缓存。"""
    os.makedirs(os.path.dirname(CACHED_NODES_FILE), exist_ok=True)
    safe = [{k: v for k, v in n.items() if k != "raw"} for n in nodes]
    with open(CACHED_NODES_FILE, "w", encoding="utf-8") as f:
        json.dump(safe, f, indent=2, ensure_ascii=False)


def _load_nodes_from_disk() -> list:
    """从磁盘加载缓存的订阅节点列表。"""
    try:
        if os.path.exists(CACHED_NODES_FILE):
            with open(CACHED_NODES_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return []


def _run_country_detection_bg(nodes: list):
    """后台线程：检测所有节点出口国家，完成后更新缓存并重排轮换池。"""
    global _cached_nodes, _detect_status
    _detect_status["running"] = True
    _detect_status["done"] = 0
    _detect_status["total"] = len(nodes)
    try:
        enriched = detect_all(nodes)
        priority = _get_priority()
        sorted_nodes = sort_nodes_by_priority(enriched, priority)
        _cached_nodes = sorted_nodes
        _save_nodes_to_disk(sorted_nodes)
        proxy_state.set_nodes(sorted_nodes)
        _detect_status["done"] = len(sorted_nodes)
        _detect_status["last_run"] = time.strftime("%H:%M:%S")
    except Exception as e:
        pass
    finally:
        _detect_status["running"] = False


def restore_active_node():
    """
    启动时调用：
    1. 从磁盘加载缓存的订阅节点，注入 proxy_state 供 429 自动轮换使用。
    2. 从磁盘恢复上次选中的节点并自动启动 xray。
    支持三种模式：xray_custom、xray（订阅节点）、manual。
    """
    global _cached_nodes

    # ── 步骤1：加载订阅节点缓存到内存 + proxy_state（轮换备用）──────────────────
    disk_nodes = _load_nodes_from_disk()
    if disk_nodes:
        priority = _get_priority()
        sorted_nodes = sort_nodes_by_priority(disk_nodes, priority)
        _cached_nodes = sorted_nodes
        proxy_state.set_nodes(sorted_nodes)  # 注入轮换池（已按优先级排序）

    # ── 步骤2：恢复上次激活的代理 ───────────────────────────────────────────────
    if not os.path.exists(ACTIVE_NODE_FILE):
        return
    try:
        with open(ACTIVE_NODE_FILE, "r", encoding="utf-8") as f:
            record = json.load(f)
    except Exception:
        return

    mode = record.get("mode")

    if mode == "xray_custom":
        index = record.get("index", 0)
        nodes = _load_custom_nodes()
        if index < 0 or index >= len(nodes):
            return
        entry = nodes[index]
        if entry["type"] == "xray_json":
            ok, _ = start_xray_from_outbounds(entry["outbounds"])
        else:
            ok, _ = start_xray(entry["node"])
        if ok:
            proxy_state.set_proxy("socks5://127.0.0.1:1080")

    elif mode == "xray":
        node = record.get("node")
        if node:
            ok, _ = start_xray(node)
            if ok:
                proxy_state.set_proxy("socks5://127.0.0.1:1080")

    elif mode == "manual":
        proxy = record.get("proxy")
        if proxy:
            proxy_state.set_proxy(proxy)
            stop_xray()


# ── 自定义节点持久化 ──────────────────────────────────────────────────────────

def _load_custom_nodes() -> list:
    try:
        if os.path.exists(CUSTOM_NODES_FILE):
            with open(CUSTOM_NODES_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return []


def _save_custom_nodes(nodes: list):
    os.makedirs(os.path.dirname(CUSTOM_NODES_FILE), exist_ok=True)
    with open(CUSTOM_NODES_FILE, "w", encoding="utf-8") as f:
        json.dump(nodes, f, indent=2, ensure_ascii=False)


def _parse_custom_config(raw: str) -> dict:
    """
    解析用户粘贴的配置，返回内部节点对象。
    支持：
      - 完整 xray JSON（含 outbounds 数组）
      - 单个 outbound JSON 对象
      - outbounds 数组 JSON
      - vless:// / vmess:// URL
    """
    raw = raw.strip()

    # vless / vmess URL
    if raw.startswith("vless://"):
        node = parse_vless(raw)
        if not node:
            raise ValueError("vless URL 解析失败")
        return {"type": "vless_url", "node": node}
    if raw.startswith("vmess://"):
        node = parse_vmess(raw)
        if not node:
            raise ValueError("vmess URL 解析失败")
        return {"type": "vmess_url", "node": node}

    # JSON 格式
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"JSON 解析失败: {e}")

    if isinstance(data, list):
        # outbounds 数组
        return {"type": "xray_json", "outbounds": data}

    if isinstance(data, dict):
        if "outbounds" in data:
            # 完整 xray 配置，提取 outbounds
            return {"type": "xray_json", "outbounds": data["outbounds"]}
        if "protocol" in data:
            # 单个 outbound 对象
            return {"type": "xray_json", "outbounds": [data]}
        raise ValueError("无法识别 JSON 格式：需要含 outbounds 的完整配置或单个 outbound 对象")

    raise ValueError("无法识别的配置格式")


def _display_info(entry: dict) -> dict:
    """从保存的条目中提取展示用信息"""
    t = entry.get("type", "?")
    if t == "xray_json":
        obs = entry.get("outbounds", [])
        first = next((o for o in obs if o.get("tag") not in ("direct", "block", "proxy3")), obs[0] if obs else {})
        proto = first.get("protocol", "?")
        try:
            addr = first["settings"]["vnext"][0]["address"]
            port = first["settings"]["vnext"][0]["port"]
            detail = f"{addr}:{port}"
        except Exception:
            detail = proto
        has_frag = any("fragment" in str(o) for o in obs)
        return {"protocol": proto, "detail": detail, "frag": has_frag, "outbound_count": len(obs)}
    else:
        node = entry.get("node", {})
        return {"protocol": node.get("protocol", "?"), "detail": f"{node.get('address','')}:{node.get('port','')}",
                "frag": False, "outbound_count": 1}


# ── 路由 ──────────────────────────────────────────────────────────────────────

@router.get("", response_class=HTMLResponse)
async def ui():
    return HTMLResponse(content=FRONTEND_HTML)


@router.get("/list")
async def list_nodes(refresh: bool = False):
    global _cached_nodes
    if refresh:
        # 手动刷新：重新从订阅URL拉取，立即返回；后台异步检测国家
        try:
            raw_nodes = fetch_and_parse(SUB_URL)
        except Exception as e:
            if not _cached_nodes:
                return JSONResponse({"error": str(e), "nodes": []}, status_code=502)
            raw_nodes = _cached_nodes  # 拉取失败保持旧列表

        # 把已有的 country/colo 数据合并过来（避免刷新后国家列全部清空）
        old_map = {n.get("server"): n for n in _cached_nodes}
        merged = []
        for n in raw_nodes:
            old = old_map.get(n.get("server"), {})
            merged_node = dict(n)
            if "country" not in merged_node and "country" in old:
                merged_node["country"] = old["country"]
                merged_node["colo"] = old.get("colo", "??")
                merged_node["exit_ip"] = old.get("exit_ip", "")
            merged.append(merged_node)

        priority = _get_priority()
        _cached_nodes = sort_nodes_by_priority(merged, priority)
        _save_nodes_to_disk(_cached_nodes)
        proxy_state.set_nodes(_cached_nodes)

        # 后台检测出口国家（不阻塞返回）
        if not _detect_status["running"]:
            t = threading.Thread(target=_run_country_detection_bg, args=(_cached_nodes,), daemon=True)
            t.start()

    elif not _cached_nodes:
        # 内存没有：先尝试磁盘缓存（不联网）
        disk = _load_nodes_from_disk()
        if disk:
            priority = _get_priority()
            _cached_nodes = sort_nodes_by_priority(disk, priority)
            proxy_state.set_nodes(_cached_nodes)
        else:
            # 磁盘也没有：首次使用，必须联网
            try:
                raw_nodes = fetch_and_parse(SUB_URL)
                _cached_nodes = raw_nodes
                _save_nodes_to_disk(_cached_nodes)
                proxy_state.set_nodes(_cached_nodes)
                # 后台检测
                if not _detect_status["running"]:
                    t = threading.Thread(target=_run_country_detection_bg, args=(_cached_nodes,), daemon=True)
                    t.start()
            except Exception as e:
                return JSONResponse({"error": str(e), "nodes": []}, status_code=502)

    safe = [{k: v for k, v in n.items() if k != "raw"} for n in _cached_nodes]
    return {"nodes": safe, "total": len(safe), "from_cache": not refresh,
            "detecting": _detect_status["running"]}


@router.get("/detect-status")
async def detect_status():
    return _detect_status


@router.get("/settings")
async def get_settings():
    s = _load_settings()
    priority = s.get("country_priority", DEFAULT_COUNTRY_PRIORITY)
    return {
        "country_priority": priority,
        "country_names": {c: COUNTRY_NAMES.get(c, c) for c in priority},
        "available_countries": COUNTRY_NAMES,
    }


@router.post("/settings")
async def save_settings(request: Request):
    body = await request.json()
    priority = body.get("country_priority")
    if not isinstance(priority, list) or not all(isinstance(c, str) for c in priority):
        return JSONResponse({"ok": False, "error": "country_priority 必须是字符串列表"}, status_code=400)
    _save_settings({"country_priority": priority})
    # 重新排序当前缓存节点
    global _cached_nodes
    if _cached_nodes:
        _cached_nodes = sort_nodes_by_priority(_cached_nodes, priority)
        _save_nodes_to_disk(_cached_nodes)
        proxy_state.set_nodes(_cached_nodes)
    return {"ok": True, "country_priority": priority}


@router.post("/select")
async def select_node(request: Request):
    body = await request.json()
    manual = body.get("manual", "").strip()
    node_index = body.get("index")

    if manual:
        proxy_state.set_proxy(manual)
        stop_xray()
        _save_active_node({"mode": "manual", "proxy": manual})
        return {"ok": True, "proxy": manual, "mode": "manual"}

    if node_index is not None:
        global _cached_nodes
        if not _cached_nodes:
            try:
                _cached_nodes = fetch_and_parse(SUB_URL)
            except Exception as e:
                return JSONResponse({"ok": False, "error": f"加载节点失败: {e}"}, status_code=500)

        try:
            node = _cached_nodes[int(node_index)]
        except (IndexError, ValueError):
            return JSONResponse({"ok": False, "error": "节点索引无效"}, status_code=400)

        ok, err = start_xray(node)
        if ok:
            proxy_state.set_proxy("socks5://127.0.0.1:1080")
            proxy_state.set_nodes(_cached_nodes, int(node_index))
            _save_active_node({"mode": "xray", "node": {k: v for k, v in node.items() if k != "raw"}})
            return {"ok": True, "proxy": "socks5://127.0.0.1:1080", "node": node.get("name"), "mode": "xray"}
        else:
            return JSONResponse({"ok": False, "error": err}, status_code=500)

    return JSONResponse({"ok": False, "error": "请提供 index 或 manual"}, status_code=400)


# ── 自定义节点 API ─────────────────────────────────────────────────────────────

@router.get("/custom/list")
async def custom_list():
    nodes = _load_custom_nodes()
    result = []
    for i, entry in enumerate(nodes):
        info = _display_info(entry)
        result.append({
            "index": i,
            "name": entry.get("name", f"自定义节点 {i+1}"),
            "type": entry.get("type", "?"),
            "added_at": entry.get("added_at", ""),
            **info,
        })
    return {"nodes": result, "total": len(result)}


@router.post("/custom/add")
async def custom_add(request: Request):
    body = await request.json()
    name = body.get("name", "").strip() or f"自定义节点"
    raw = body.get("config", "").strip()
    if not raw:
        return JSONResponse({"ok": False, "error": "配置内容不能为空"}, status_code=400)

    try:
        parsed = _parse_custom_config(raw)
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)

    nodes = _load_custom_nodes()
    entry = {
        "name": name,
        "added_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        **parsed,
    }
    nodes.append(entry)
    _save_custom_nodes(nodes)
    return {"ok": True, "total": len(nodes), "index": len(nodes) - 1}


@router.delete("/custom/{index}")
async def custom_delete(index: int):
    nodes = _load_custom_nodes()
    if index < 0 or index >= len(nodes):
        return JSONResponse({"ok": False, "error": "索引无效"}, status_code=400)
    removed = nodes.pop(index)
    _save_custom_nodes(nodes)
    return {"ok": True, "removed": removed.get("name", "?")}


@router.post("/custom/select/{index}")
async def custom_select(index: int):
    nodes = _load_custom_nodes()
    if index < 0 or index >= len(nodes):
        return JSONResponse({"ok": False, "error": "索引无效"}, status_code=400)

    entry = nodes[index]
    node_name = entry.get("name", f"自定义节点 {index+1}")

    if entry["type"] == "xray_json":
        outbounds = entry["outbounds"]
        ok, err = start_xray_from_outbounds(outbounds)
        if ok:
            proxy_state.set_proxy("socks5://127.0.0.1:1080")
            _save_active_node({"mode": "xray_custom", "index": index})
            return {"ok": True, "proxy": "socks5://127.0.0.1:1080", "node": node_name, "mode": "xray_custom"}
        return JSONResponse({"ok": False, "error": err}, status_code=500)
    else:
        node = entry["node"]
        ok, err = start_xray(node)
        if ok:
            proxy_state.set_proxy("socks5://127.0.0.1:1080")
            _save_active_node({"mode": "xray_custom", "index": index})
            return {"ok": True, "proxy": "socks5://127.0.0.1:1080", "node": node_name, "mode": "xray"}
        return JSONResponse({"ok": False, "error": err}, status_code=500)


@router.get("/status")
async def status():
    proxy = proxy_state.get_proxy()
    xray_running = is_running()
    google_ok = False
    latency_ms = -1

    if proxy:
        try:
            import socket
            from urllib.parse import urlparse
            parsed = urlparse(proxy)
            host = parsed.hostname or "127.0.0.1"
            port = parsed.port or 1080
            t0 = time.time()
            s = socket.create_connection((host, port), timeout=5)
            s.close()
            latency_ms = int((time.time() - t0) * 1000)
            google_ok = True
        except Exception:
            google_ok = False

    return {
        "proxy": proxy,
        "xray_running": xray_running,
        "google_reachable": google_ok,
        "latency_ms": latency_ms,
    }


@router.get("/logs")
async def logs():
    return {"logs": get_logs(100)}


@router.post("/clear")
async def clear_proxy():
    stop_xray()
    proxy_state.set_proxy(None)
    return {"ok": True}


# ── 前端 HTML ─────────────────────────────────────────────────────────────────

FRONTEND_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>代理管理器</title>
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: #0f172a; color: #e2e8f0; min-height: 100vh; }
.header { background: #1e293b; border-bottom: 1px solid #334155; padding: 16px 24px; display: flex; align-items: center; gap: 12px; }
.header h1 { font-size: 18px; font-weight: 600; }
.badge { background: #3b82f6; color: #fff; font-size: 11px; padding: 2px 8px; border-radius: 99px; }
.main { max-width: 1100px; margin: 0 auto; padding: 24px 16px; display: grid; gap: 20px; }
.card { background: #1e293b; border: 1px solid #334155; border-radius: 12px; padding: 20px; }
.card-title { font-size: 14px; font-weight: 600; color: #94a3b8; text-transform: uppercase; letter-spacing: .05em; margin-bottom: 14px; display: flex; align-items: center; justify-content: space-between; }
.status-row { display: flex; align-items: center; gap: 16px; flex-wrap: wrap; }
.indicator { display: flex; align-items: center; gap: 6px; font-size: 14px; }
.dot { width: 10px; height: 10px; border-radius: 50%; }
.dot-green { background: #22c55e; box-shadow: 0 0 8px #22c55e88; }
.dot-red { background: #ef4444; }
.dot-gray { background: #64748b; }
.dot-yellow { background: #f59e0b; }
.proxy-text { font-family: monospace; font-size: 13px; color: #7dd3fc; background: #0f172a; padding: 4px 10px; border-radius: 6px; }
.btn { display: inline-flex; align-items: center; gap: 6px; padding: 8px 16px; border-radius: 8px; font-size: 13px; font-weight: 500; cursor: pointer; border: none; transition: .15s; }
.btn-primary { background: #3b82f6; color: #fff; }
.btn-primary:hover { background: #2563eb; }
.btn-success { background: #16a34a; color: #fff; }
.btn-success:hover { background: #15803d; }
.btn-danger { background: #dc2626; color: #fff; }
.btn-danger:hover { background: #b91c1c; }
.btn-ghost { background: #334155; color: #cbd5e1; }
.btn-ghost:hover { background: #475569; }
.btn-warn { background: #92400e; color: #fcd34d; }
.btn-warn:hover { background: #78350f; }
.btn:disabled { opacity: .5; cursor: not-allowed; }
.btn-sm { padding: 4px 10px; font-size: 12px; }
.btn-row { display: flex; gap: 10px; flex-wrap: wrap; }
table { width: 100%; border-collapse: collapse; font-size: 13px; }
th { text-align: left; color: #64748b; font-weight: 500; padding: 8px 12px; border-bottom: 1px solid #334155; }
td { padding: 10px 12px; border-bottom: 1px solid #1e293b; }
tr:last-child td { border-bottom: none; }
tr:hover td { background: #0f172a44; }
.proto-badge { font-size: 11px; padding: 2px 7px; border-radius: 4px; font-weight: 600; }
.proto-vless { background: #1d4ed833; color: #60a5fa; border: 1px solid #1d4ed866; }
.proto-vmess { background: #7c3aed33; color: #a78bfa; border: 1px solid #7c3aed866; }
.manual-row { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }
.input { background: #0f172a; border: 1px solid #334155; color: #e2e8f0; padding: 8px 12px; border-radius: 8px; font-size: 13px; font-family: monospace; flex: 1; min-width: 240px; outline: none; }
.input:focus { border-color: #3b82f6; }
.input-name { background: #0f172a; border: 1px solid #334155; color: #e2e8f0; padding: 8px 12px; border-radius: 8px; font-size: 13px; outline: none; width: 220px; }
.input-name:focus { border-color: #3b82f6; }
textarea.input { font-family: monospace; font-size: 12px; resize: vertical; min-height: 140px; line-height: 1.5; }
.log-box { background: #0a0f1e; border: 1px solid #1e3a5f; border-radius: 8px; padding: 14px; font-family: monospace; font-size: 12px; color: #7dd3fc; max-height: 280px; overflow-y: auto; white-space: pre-wrap; word-break: break-all; }
.loading { color: #64748b; font-style: italic; }
.tag { font-size: 11px; padding: 1px 6px; border-radius: 4px; }
.tag-tls { background: #16a34a33; color: #86efac; }
.tag-ws { background: #d9770633; color: #fcd34d; }
.tag-reality { background: #9333ea33; color: #d8b4fe; }
.tag-frag { background: #7c3aed33; color: #c4b5fd; }
.tag-custom { background: #0e7490; color: #a5f3fc; }
.alert { padding: 10px 14px; border-radius: 8px; font-size: 13px; margin-bottom: 12px; display: none; }
.alert-error { background: #7f1d1d44; border: 1px solid #dc262666; color: #fca5a5; }
.alert-success { background: #14532d44; border: 1px solid #16a34a66; color: #86efac; }
.lm { color: #94a3b8; }
.add-form { display: grid; gap: 12px; }
.add-form-row { display: flex; gap: 10px; align-items: flex-start; flex-wrap: wrap; }
.hint { font-size: 12px; color: #64748b; margin-top: 4px; }
.empty-state { text-align: center; padding: 24px; color: #475569; font-size: 13px; }
</style>
</head>
<body>
<div class="header">
  <svg width="22" height="22" fill="none" viewBox="0 0 24 24"><path stroke="#3b82f6" stroke-width="2" stroke-linecap="round" d="M12 2a10 10 0 1 0 0 20A10 10 0 0 0 12 2zm0 0c-2.5 2.5-4 6-4 10s1.5 7.5 4 10m0-20c2.5 2.5 4 6 4 10s-1.5 7.5-4 10M2 12h20"/></svg>
  <h1>代理管理器</h1>
  <span class="badge">Vertex AI Proxy</span>
</div>
<div class="main">

  <!-- 状态栏 -->
  <div class="card">
    <div class="card-title">当前状态</div>
    <div class="status-row">
      <div class="indicator"><div class="dot dot-gray" id="dot-xray"></div><span id="lbl-xray">xray 未运行</span></div>
      <div class="indicator"><div class="dot dot-gray" id="dot-google"></div><span id="lbl-google">Google 未测试</span></div>
      <div id="proxy-display" style="display:none;"><span class="proxy-text" id="proxy-text"></span></div>
      <span class="lm" id="latency-text"></span>
      <div class="btn-row" style="margin-left:auto">
        <button class="btn btn-primary" onclick="testProxy()">⚡ 测试当前代理</button>
        <button class="btn btn-danger" onclick="clearProxy()">✕ 清除代理</button>
      </div>
    </div>
  </div>

  <!-- 自定义节点 -->
  <div class="card">
    <div class="card-title">
      <span>⭐ 我的自定义节点</span>
      <button class="btn btn-ghost btn-sm" onclick="loadCustom()">刷新</button>
    </div>
    <div id="custom-alert" class="alert"></div>
    <div id="custom-table"><span class="loading">加载中…</span></div>

    <!-- 添加表单 -->
    <div style="margin-top:18px;padding-top:16px;border-top:1px solid #334155;">
      <div style="font-size:13px;font-weight:600;color:#94a3b8;margin-bottom:12px;">添加新节点</div>
      <div class="add-form">
        <div class="add-form-row">
          <div>
            <input class="input-name" id="custom-name" placeholder="节点名称（可选）">
          </div>
        </div>
        <div>
          <textarea class="input" id="custom-config" placeholder="粘贴以下任意格式：
① vless://... 链接
② vmess://... 链接  
③ xray 完整 JSON 配置（含 outbounds 数组）
④ 单个 outbound JSON 对象"></textarea>
          <div class="hint">支持带 TLS 分片（fragment）的完整 xray JSON 配置</div>
        </div>
        <div>
          <button class="btn btn-success" onclick="addCustom()">＋ 添加保存</button>
        </div>
      </div>
    </div>
  </div>

  <!-- 手动输入 -->
  <div class="card">
    <div class="card-title">手动输入代理地址</div>
    <div class="manual-row">
      <input class="input" id="manual-input" placeholder="socks5://127.0.0.1:1080  或  http://user:pass@ip:port">
      <button class="btn btn-success" onclick="applyManual()">应用</button>
    </div>
  </div>

  <!-- 国家优先级设置 -->
  <div class="card">
    <div class="card-title">🌍 国家优先级设置
      <span class="lm" style="font-size:12px;font-weight:400">429时按此顺序自动切换节点</span>
    </div>
    <div id="priority-list" style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:12px"></div>
    <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
      <input class="input" id="priority-input" placeholder="国家代码，如 JP,KR,AU,SG,US" style="max-width:320px">
      <button class="btn btn-primary btn-sm" onclick="savePriority()">保存排列</button>
      <button class="btn btn-ghost btn-sm" onclick="loadSettings()">重置</button>
    </div>
    <div style="margin-top:8px;font-size:12px;color:#64748b">
      常用代码：JP=日本 KR=韩国 AU=澳大利亚 SG=新加坡 HK=香港 TW=台湾 GB=英国 DE=德国 US=美国
    </div>
  </div>

  <!-- 订阅节点列表 -->
  <div class="card">
    <div class="card-title">订阅节点列表</div>
    <div id="alert-box" class="alert"></div>
    <div class="btn-row" style="margin-bottom:14px">
      <button class="btn btn-ghost" onclick="loadNodes(true)" id="btn-refresh">🔄 刷新节点列表</button>
      <span class="lm" id="node-count"></span>
      <span id="detect-status" style="font-size:12px;color:#f59e0b;display:none">⏳ 正在检测出口国家…</span>
    </div>
    <div id="table-container"><span class="loading">点击"刷新节点列表"加载节点…</span></div>
  </div>

  <!-- 错误日志 -->
  <div class="card">
    <div class="card-title">
      运行日志
      <button class="btn btn-ghost btn-sm" onclick="loadLogs()">刷新</button>
    </div>
    <div class="log-box" id="log-box">-- 日志为空 --</div>
  </div>

</div>

<script>
let nodes = [];
let customNodes = [];

async function api(path, opts = {}) {
  const r = await fetch('/proxy-manager' + path, {
    headers: { 'Content-Type': 'application/json', 'Authorization': 'Bearer sk-123456' },
    ...opts
  });
  return r.json();
}

function showAlert(msg, type = 'error', elId = 'alert-box') {
  const el = document.getElementById(elId);
  el.className = 'alert alert-' + type;
  el.textContent = msg;
  el.style.display = 'block';
  setTimeout(() => el.style.display = 'none', 6000);
}

function escHtml(s) {
  return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

// ── 状态 ─────────────────────────────────────────────────────────────────────

async function loadStatus() {
  try {
    const d = await api('/status');
    const dotX = document.getElementById('dot-xray');
    const lblX = document.getElementById('lbl-xray');
    const dotG = document.getElementById('dot-google');
    const lblG = document.getElementById('lbl-google');
    dotX.className = 'dot ' + (d.xray_running ? 'dot-green' : 'dot-gray');
    lblX.textContent = d.xray_running ? 'xray 运行中' : 'xray 未运行';
    dotG.className = 'dot ' + (d.google_reachable ? 'dot-green' : (d.proxy ? 'dot-red' : 'dot-gray'));
    lblG.textContent = d.google_reachable
      ? 'SOCKS5 端口可达' + (d.latency_ms > 0 ? ' (' + d.latency_ms + 'ms)' : '')
      : (d.proxy ? '代理端口不可达' : '未设置代理');
    if (d.proxy) {
      document.getElementById('proxy-display').style.display = 'block';
      document.getElementById('proxy-text').textContent = d.proxy;
    } else {
      document.getElementById('proxy-display').style.display = 'none';
    }
  } catch(e) { console.error(e); }
}

async function testProxy() {
  const dotG = document.getElementById('dot-google');
  const lblG = document.getElementById('lbl-google');
  dotG.className = 'dot dot-yellow';
  lblG.textContent = '测试中…';
  await loadStatus();
}

async function clearProxy() {
  await api('/clear', { method: 'POST', body: '{}' });
  await loadStatus();
}

// ── 自定义节点 ────────────────────────────────────────────────────────────────

async function loadCustom() {
  const d = await api('/custom/list');
  customNodes = d.nodes || [];
  renderCustom();
}

function renderCustom() {
  const el = document.getElementById('custom-table');
  if (!customNodes.length) {
    el.innerHTML = '<div class="empty-state">还没有自定义节点 — 在下方粘贴配置后点击添加</div>';
    return;
  }
  let html = '<table><thead><tr><th>#</th><th>名称</th><th>协议</th><th>地址</th><th>特性</th><th>添加时间</th><th>操作</th></tr></thead><tbody>';
  customNodes.forEach((n, i) => {
    const protoCls = n.protocol === 'vless' ? 'proto-vless' : 'proto-vmess';
    const fragTag = n.frag ? '<span class="tag tag-frag">分片</span>' : '';
    const customTag = n.type === 'xray_json' ? '<span class="tag tag-custom">xray</span>' : '';
    html += `<tr>
      <td class="lm">${i+1}</td>
      <td><strong>${escHtml(n.name)}</strong></td>
      <td><span class="proto-badge ${protoCls}">${escHtml(n.protocol||'?').toUpperCase()}</span></td>
      <td style="font-family:monospace;font-size:12px">${escHtml(n.detail)}</td>
      <td>${fragTag} ${customTag}</td>
      <td class="lm" style="font-size:12px">${escHtml(n.added_at)}</td>
      <td>
        <div class="btn-row">
          <button class="btn btn-success btn-sm" onclick="selectCustom(${i})">选择</button>
          <button class="btn btn-warn btn-sm" onclick="deleteCustom(${i})">删除</button>
        </div>
      </td>
    </tr>`;
  });
  html += '</tbody></table>';
  el.innerHTML = html;
}

async function selectCustom(i) {
  const name = customNodes[i]?.name || `节点 ${i+1}`;
  showAlert(`正在启动: ${name} …`, 'success', 'custom-alert');
  try {
    const d = await api(`/custom/select/${i}`, { method: 'POST', body: '{}' });
    if (d.ok) {
      showAlert(`✅ 已切换到: ${d.node}`, 'success', 'custom-alert');
      await loadStatus();
      await loadLogs();
    } else {
      showAlert('启动失败: ' + d.error, 'error', 'custom-alert');
    }
  } catch(e) { showAlert('请求异常: ' + e, 'error', 'custom-alert'); }
}

async function deleteCustom(i) {
  const name = customNodes[i]?.name || `节点 ${i+1}`;
  if (!confirm(`确认删除节点「${name}」？`)) return;
  const d = await api(`/custom/${i}`, { method: 'DELETE' });
  if (d.ok) {
    showAlert(`已删除: ${d.removed}`, 'success', 'custom-alert');
    await loadCustom();
  } else {
    showAlert('删除失败: ' + d.error, 'error', 'custom-alert');
  }
}

async function addCustom() {
  const name = document.getElementById('custom-name').value.trim();
  const config = document.getElementById('custom-config').value.trim();
  if (!config) { showAlert('请粘贴配置内容', 'error', 'custom-alert'); return; }
  const d = await api('/custom/add', {
    method: 'POST',
    body: JSON.stringify({ name: name || '自定义节点', config })
  });
  if (d.ok) {
    showAlert(`✅ 已添加，共 ${d.total} 个自定义节点`, 'success', 'custom-alert');
    document.getElementById('custom-name').value = '';
    document.getElementById('custom-config').value = '';
    await loadCustom();
  } else {
    showAlert('添加失败: ' + d.error, 'error', 'custom-alert');
  }
}

// ── 手动代理 ──────────────────────────────────────────────────────────────────

async function applyManual() {
  const val = document.getElementById('manual-input').value.trim();
  if (!val) return showAlert('请输入代理地址');
  const d = await api('/select', { method: 'POST', body: JSON.stringify({ manual: val }) });
  if (d.ok) { showAlert('手动代理已应用: ' + val, 'success'); await loadStatus(); }
  else showAlert('应用失败: ' + d.error);
}

// ── 设置（国家优先级）────────────────────────────────────────────────────────

async function loadSettings() {
  try {
    const d = await api('/settings');
    const priority = d.country_priority || [];
    document.getElementById('priority-input').value = priority.join(',');
    const names = d.country_names || {};
    const listEl = document.getElementById('priority-list');
    listEl.innerHTML = priority.map((c, i) =>
      `<span style="background:#1e293b;border:1px solid #334155;border-radius:6px;padding:4px 10px;font-size:13px">
        <span style="color:#64748b;margin-right:4px">${i+1}.</span>${names[c] || c}
      </span>`
    ).join('');
  } catch(e) {}
}

async function savePriority() {
  const raw = document.getElementById('priority-input').value.trim().toUpperCase();
  const priority = raw.split(/[,\s]+/).filter(Boolean);
  if (!priority.length) { showAlert('请输入至少一个国家代码'); return; }
  try {
    const d = await api('/settings', { method: 'POST', body: JSON.stringify({ country_priority: priority }) });
    if (d.ok) {
      showAlert('✅ 国家优先级已保存，轮换顺序已更新', 'success');
      await loadSettings();
      await loadNodes();
    } else {
      showAlert('保存失败: ' + d.error);
    }
  } catch(e) { showAlert('保存失败: ' + e); }
}

// ── 订阅节点 ──────────────────────────────────────────────────────────────────

let detectPollTimer = null;

async function pollDetectStatus() {
  try {
    const d = await api('/detect-status');
    const el = document.getElementById('detect-status');
    if (d.running) {
      el.style.display = 'inline';
      el.textContent = `⏳ 正在检测出口国家… ${d.done}/${d.total}`;
    } else {
      el.style.display = d.last_run ? 'inline' : 'none';
      el.style.color = '#22c55e';
      el.textContent = d.last_run ? `✅ 国家检测完成 (${d.last_run})` : '';
      if (detectPollTimer) { clearInterval(detectPollTimer); detectPollTimer = null; }
      // 检测完成后刷新表格
      if (d.last_run && nodes.length) { await loadNodes(); }
    }
  } catch(e) {}
}

async function loadNodes(refresh = false) {
  const btn = document.getElementById('btn-refresh');
  btn.disabled = true; btn.textContent = '加载中…';
  if (refresh) document.getElementById('table-container').innerHTML = '<span class="loading">正在拉取订阅…</span>';
  try {
    const d = await api('/list' + (refresh ? '?refresh=true' : ''));
    if (d.error) { showAlert('拉取失败: ' + d.error); return; }
    nodes = d.nodes;
    document.getElementById('node-count').textContent = `共 ${d.total} 个节点`;
    renderTable();
    // 如果正在检测，启动轮询
    if (d.detecting && !detectPollTimer) {
      document.getElementById('detect-status').style.display = 'inline';
      document.getElementById('detect-status').style.color = '#f59e0b';
      detectPollTimer = setInterval(pollDetectStatus, 3000);
    }
  } finally {
    btn.disabled = false; btn.textContent = '🔄 刷新节点列表';
  }
}

function countryBadge(n) {
  if (!n.country || n.country === '??') return '<span class="lm" style="font-size:11px">检测中…</span>';
  const colo = n.colo || '??';
  const flag = n.country === 'JP' ? '🇯🇵' : n.country === 'KR' ? '🇰🇷' :
               n.country === 'AU' ? '🇦🇺' : n.country === 'SG' ? '🇸🇬' :
               n.country === 'HK' ? '🇭🇰' : n.country === 'TW' ? '🇹🇼' :
               n.country === 'US' ? '🇺🇸' : n.country === 'GB' ? '🇬🇧' :
               n.country === 'DE' ? '🇩🇪' : n.country === 'NL' ? '🇳🇱' :
               n.country === 'FR' ? '🇫🇷' : n.country === 'CA' ? '🇨🇦' : '🌐';
  return `<span style="font-size:12px">${flag} <span style="color:#64748b;font-size:11px">${colo}</span></span>`;
}

function renderTable() {
  if (!nodes.length) {
    document.getElementById('table-container').innerHTML = '<span class="loading">没有找到节点</span>';
    return;
  }
  let html = '<table><thead><tr><th>#</th><th>国家</th><th>名称</th><th>地址</th><th>端口</th><th>协议</th><th>传输</th><th>操作</th></tr></thead><tbody>';
  nodes.forEach((n, i) => {
    const protoCls = n.protocol === 'vless' ? 'proto-vless' : 'proto-vmess';
    const netTag = n.network && n.network !== 'tcp' ? `<span class="tag tag-ws">${n.network}</span>` : '<span class="lm">tcp</span>';
    html += `<tr>
      <td class="lm">${i+1}</td>
      <td>${countryBadge(n)}</td>
      <td>${escHtml(n.name||'')}</td>
      <td style="font-family:monospace;font-size:12px">${escHtml((n.server||n.address||''))}</td>
      <td>${n.port}</td>
      <td><span class="proto-badge ${protoCls}">${(n.protocol||'').toUpperCase()}</span></td>
      <td>${netTag}</td>
      <td><button class="btn btn-success btn-sm" onclick="selectNode(${i})">选择</button></td>
    </tr>`;
  });
  html += '</tbody></table>';
  document.getElementById('table-container').innerHTML = html;
}

async function selectNode(i) {
  const btns = document.querySelectorAll('button');
  btns.forEach(b => b.disabled = true);
  showAlert(`正在启动节点: ${nodes[i].name} …`, 'success');
  try {
    const d = await api('/select', { method: 'POST', body: JSON.stringify({ index: i }) });
    if (d.ok) {
      showAlert(`✅ 节点已选择: ${d.node}，代理: ${d.proxy}`, 'success');
      await loadStatus();
    } else {
      showAlert('启动失败: ' + d.error);
    }
  } finally {
    btns.forEach(b => b.disabled = false);
    await loadLogs();
  }
}

// ── 日志 ──────────────────────────────────────────────────────────────────────

async function loadLogs() {
  try {
    const d = await api('/logs');
    const box = document.getElementById('log-box');
    box.textContent = d.logs || '-- 日志为空 --';
    box.scrollTop = box.scrollHeight;
  } catch(e) {}
}

// ── 初始化 ────────────────────────────────────────────────────────────────────

loadStatus();
loadCustom();
loadLogs();
loadSettings();
setInterval(loadStatus, 8000);
setInterval(loadLogs, 15000);
</script>
</body>
</html>"""
