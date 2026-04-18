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

import secrets as _secrets
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials

_admin_security = HTTPBasic(auto_error=False)

def _admin_auth(creds: HTTPBasicCredentials | None = Depends(_admin_security)):
    """管理界面访问控制：设置 ADMIN_PASSWORD 环境变量后启用 HTTP Basic 认证"""
    expected = os.environ.get("ADMIN_PASSWORD", "").strip()
    if not expected:
        # 未设置密码：放行（开发环境）
        return
    if creds is None or not _secrets.compare_digest(creds.password, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="需要管理员密码",
            headers={"WWW-Authenticate": "Basic"},
        )

router = APIRouter(prefix="/proxy-manager", tags=["proxy-manager"], dependencies=[Depends(_admin_auth)])

_DEFAULT_SUB_URL = "https://tian110110.us.ci/sub?token=e2fb1e6322ce2a3d02e0d28de5846ea6"
SUB_URL = os.environ.get("SUB_URL", "").strip() or _DEFAULT_SUB_URL
_cached_nodes: list = []
_detect_status: dict = {"running": False, "done": 0, "total": 0, "last_run": ""}

PROJ_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CUSTOM_NODES_FILE = os.path.join(PROJ_ROOT, "config", "custom_nodes.json")
ACTIVE_NODE_FILE = os.path.join(PROJ_ROOT, "config", "active_node.json")
CACHED_NODES_FILE = os.path.join(PROJ_ROOT, "config", "cached_nodes.json")
SETTINGS_FILE = os.path.join(PROJ_ROOT, "config", "settings.json")
SUB_URLS_FILE = os.path.join(PROJ_ROOT, "config", "sub_urls.json")

_bench_running: bool = False
_bench_results: dict = {}  # index -> latency_ms or None

_quota_running: bool = False
_quota_results: dict = {}   # index -> "ok" | "exhausted" | "dead" | "checking"
_quota_current: int = -1    # 当前正在检测的节点索引


def _load_sub_urls() -> list:
    try:
        if os.path.exists(SUB_URLS_FILE):
            with open(SUB_URLS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return [SUB_URL]


def _save_sub_urls(urls: list):
    os.makedirs(os.path.dirname(SUB_URLS_FILE), exist_ok=True)
    with open(SUB_URLS_FILE, "w", encoding="utf-8") as f:
        json.dump(urls, f, indent=2, ensure_ascii=False)


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
    fetch_results: list[dict] = []  # per-url result info

    if refresh:
        urls = _load_sub_urls()
        if not urls:
            return JSONResponse({"error": "没有订阅链接，请先在「订阅链接管理」里添加链接", "nodes": [], "fetch_results": []}, status_code=400)

        raw_nodes = []
        for url in urls:
            short = url[:60] + "…" if len(url) > 60 else url
            try:
                fetched = fetch_and_parse(url)
                raw_nodes.extend(fetched)
                fetch_results.append({"url": short, "ok": True, "count": len(fetched)})
            except Exception as e:
                fetch_results.append({"url": short, "ok": False, "error": str(e)})

        all_failed = all(not r["ok"] for r in fetch_results)
        if not raw_nodes:
            # 全部失败，如果有旧缓存就回退并附带错误信息
            if _cached_nodes:
                safe = [{k: v for k, v in n.items() if k != "raw"} for n in _cached_nodes]
                return JSONResponse({
                    "nodes": safe, "total": len(safe), "from_cache": True,
                    "detecting": _detect_status["running"],
                    "fetch_results": fetch_results,
                    "warning": "所有订阅链接拉取/解析失败，当前显示的是上次缓存的旧节点"
                })
            errors_str = " | ".join(r.get("error", "") for r in fetch_results if not r["ok"])
            return JSONResponse({"error": errors_str or "未能解析到任何节点", "nodes": [], "fetch_results": fetch_results}, status_code=502)

        # 合并已有 country/colo/latency_ms 数据
        old_map = {n.get("server"): n for n in _cached_nodes}
        merged = []
        for n in raw_nodes:
            old = old_map.get(n.get("server"), {})
            merged_node = dict(n)
            for k in ("country", "colo", "exit_ip", "latency_ms"):
                if k not in merged_node and k in old:
                    merged_node[k] = old[k]
            merged.append(merged_node)

        priority = _get_priority()
        _cached_nodes = sort_nodes_by_priority(merged, priority)
        _save_nodes_to_disk(_cached_nodes)
        proxy_state.set_nodes(_cached_nodes)

        if not _detect_status["running"]:
            t = threading.Thread(target=_run_country_detection_bg, args=(_cached_nodes,), daemon=True)
            t.start()

    elif not _cached_nodes:
        disk = _load_nodes_from_disk()
        if disk:
            priority = _get_priority()
            _cached_nodes = sort_nodes_by_priority(disk, priority)
            proxy_state.set_nodes(_cached_nodes)
        else:
            urls = _load_sub_urls()
            raw_nodes = []
            for url in urls:
                short = url[:60] + "…" if len(url) > 60 else url
                try:
                    fetched = fetch_and_parse(url)
                    raw_nodes.extend(fetched)
                    fetch_results.append({"url": short, "ok": True, "count": len(fetched)})
                except Exception as e:
                    fetch_results.append({"url": short, "ok": False, "error": str(e)})
            if not raw_nodes:
                errors_str = " | ".join(r.get("error", "") for r in fetch_results if not r["ok"])
                return JSONResponse({"error": errors_str or "拉取订阅失败，请检查订阅链接", "nodes": [], "fetch_results": fetch_results}, status_code=502)
            _cached_nodes = raw_nodes
            _save_nodes_to_disk(_cached_nodes)
            proxy_state.set_nodes(_cached_nodes)
            if not _detect_status["running"]:
                t = threading.Thread(target=_run_country_detection_bg, args=(_cached_nodes,), daemon=True)
                t.start()

    safe = [{k: v for k, v in n.items() if k != "raw"} for n in _cached_nodes]
    return {"nodes": safe, "total": len(safe), "from_cache": not refresh,
            "fetch_results": fetch_results, "detecting": _detect_status["running"]}


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


@router.get("/sub-urls")
async def sub_urls_list():
    return {"urls": _load_sub_urls()}


@router.post("/sub-urls")
async def sub_urls_add(request: Request):
    body = await request.json()
    url = (body.get("url") or "").strip()
    if not url:
        return JSONResponse({"ok": False, "error": "URL 不能为空"}, status_code=400)
    urls = _load_sub_urls()
    if url in urls:
        return JSONResponse({"ok": False, "error": "该链接已存在"}, status_code=400)
    urls.append(url)
    _save_sub_urls(urls)
    return {"ok": True, "urls": urls}


@router.delete("/sub-urls/{index}")
async def sub_urls_delete(index: int):
    urls = _load_sub_urls()
    if index < 0 or index >= len(urls):
        return JSONResponse({"ok": False, "error": "索引无效"}, status_code=400)
    removed = urls.pop(index)
    _save_sub_urls(urls)
    return {"ok": True, "removed": removed, "urls": urls}


@router.post("/bench")
async def bench_nodes():
    """TCP ping 每个节点的 server:port，并发测速，结果存回 _cached_nodes[i]['latency_ms']"""
    import asyncio
    global _bench_running, _bench_results, _cached_nodes
    if _bench_running:
        return {"ok": False, "error": "测速正在进行中，请稍候"}
    if not _cached_nodes:
        return {"ok": False, "error": "节点列表为空，请先刷新订阅"}

    _bench_running = True
    _bench_results = {}
    nodes_snap = list(_cached_nodes)

    async def _tcp_ping(host: str, port: int, timeout: float = 5.0) -> int | None:
        try:
            t0 = asyncio.get_event_loop().time()
            _, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=timeout)
            ms = int((asyncio.get_event_loop().time() - t0) * 1000)
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
            return ms
        except Exception:
            return None

    async def _run():
        global _bench_running, _bench_results, _cached_nodes
        try:
            tasks = []
            for i, n in enumerate(nodes_snap):
                host = n.get("server") or n.get("address", "")
                port = int(n.get("port", 443))
                tasks.append(_tcp_ping(host, port))
            results = await asyncio.gather(*tasks)
            _bench_results = {i: r for i, r in enumerate(results)}
            for i, n in enumerate(_cached_nodes):
                if i < len(results):
                    n["latency_ms"] = results[i]
            # 测速完后按延迟重排（None 排最后）
            def _sort_key(node):
                lat = node.get("latency_ms")
                return (0, lat) if lat is not None else (1, 9999)
            _cached_nodes.sort(key=_sort_key)
            _save_nodes_to_disk(_cached_nodes)
            proxy_state.set_nodes(_cached_nodes)
        finally:
            _bench_running = False

    asyncio.create_task(_run())
    return {"ok": True, "total": len(nodes_snap), "message": f"正在测速 {len(nodes_snap)} 个节点，几秒后刷新列表查看结果"}


@router.get("/bench-status")
async def bench_status():
    return {"running": _bench_running, "results": _bench_results}


@router.post("/quota-scan")
async def quota_scan(request: Request):
    """依次启动每个节点，发一个 HTTPS 请求检测能否连上 Google，判断节点是否可用/额度耗尽"""
    import asyncio
    global _quota_running, _quota_results, _quota_current, _cached_nodes

    if _quota_running:
        return {"ok": False, "error": "检测正在进行中，请稍候"}
    if not _cached_nodes:
        return {"ok": False, "error": "节点列表为空，请先刷新订阅"}

    body = {}
    try:
        body = await request.json()
    except Exception:
        pass
    max_nodes = int(body.get("max_nodes", 30))
    nodes_to_check = min(max_nodes, len(_cached_nodes))

    _quota_running = True
    _quota_results = {}
    _quota_current = -1
    nodes_snap = list(_cached_nodes[:nodes_to_check])

    # 轮流用这两个模型检测真实 Gemini 配额（不只是连通性）
    SCAN_MODELS = ["gemini-2.5-pro", "gemini-3.1-pro-preview"]

    def _check_node_sync(node: dict, model_name: str, timeout: float = 75.0) -> str:
        """同步：启动 xray，调本地 /v1/chat/completions 用真实 Gemini 模型试一句话"""
        try:
            ok, _msg = start_xray(node)
            if not ok:
                return "dead"
            # xray 内部已 _wait_socks5_ready，无需额外等待
            try:
                import httpx
                payload = {
                    "model": model_name,
                    "messages": [{"role": "user", "content": "hi"}],
                    "stream": False,
                    "max_tokens": 8,
                }
                headers = {
                    "Authorization": "Bearer sk-123456",
                    "Content-Type": "application/json",
                }
                with httpx.Client(timeout=timeout) as client:
                    r = client.post(
                        "http://127.0.0.1:8000/v1/chat/completions",
                        json=payload,
                        headers=headers,
                    )
                    if r.status_code == 429:
                        return "exhausted"
                    if r.status_code != 200:
                        return "dead"
                    try:
                        body = r.json()
                    except Exception:
                        return "dead"
                    # 检查是否真的有内容返回
                    choices = body.get("choices") or []
                    if not choices:
                        return "dead"
                    msg = (choices[0] or {}).get("message") or {}
                    content = (msg.get("content") or "").strip()
                    if content:
                        return "ok"
                    # 有 200 但空内容 → 节点能连但模型没出结果，视为耗尽
                    return "exhausted"
            except httpx.TimeoutException:
                return "dead"
            except Exception:
                return "dead"
        except Exception:
            return "dead"

    async def _run():
        global _quota_running, _quota_results, _quota_current, _cached_nodes
        try:
            loop = asyncio.get_event_loop()
            for i, node in enumerate(nodes_snap):
                _quota_current = i
                _quota_results[i] = "checking"
                model_name = SCAN_MODELS[i % len(SCAN_MODELS)]
                status = await loop.run_in_executor(None, _check_node_sync, node, model_name)
                _quota_results[i] = status
        finally:
            _quota_current = -1
            _quota_running = False
            # 扫完后自动切回第一个 "ok" 节点
            first_ok = next((i for i, s in _quota_results.items() if s == "ok"), None)
            if first_ok is not None and first_ok < len(_cached_nodes):
                await asyncio.get_event_loop().run_in_executor(
                    None, start_xray, _cached_nodes[first_ok]
                )

    asyncio.create_task(_run())
    return {"ok": True, "total": nodes_to_check, "message": f"正在检测前 {nodes_to_check} 个节点，请稍候…"}


@router.get("/quota-scan/status")
async def quota_scan_status():
    return {
        "running": _quota_running,
        "current": _quota_current,
        "results": _quota_results,
    }


@router.post("/quota-scan/remove-failed")
async def quota_scan_remove_failed():
    """删除所有检测结果为 dead 或 exhausted 的节点"""
    global _cached_nodes, _quota_results
    if _quota_running:
        return {"ok": False, "error": "检测仍在进行，请等待完成"}
    failed_indices = {i for i, s in _quota_results.items() if s in ("dead", "exhausted")}
    if not failed_indices:
        return {"ok": True, "removed": 0, "message": "没有需要删除的节点"}
    new_nodes = [n for i, n in enumerate(_cached_nodes) if i not in failed_indices]
    removed = len(_cached_nodes) - len(new_nodes)
    _cached_nodes = new_nodes
    _quota_results = {}
    _save_nodes_to_disk(_cached_nodes)
    proxy_state.set_nodes(_cached_nodes)
    return {"ok": True, "removed": removed, "message": f"已删除 {removed} 个无效节点"}


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
        "node_count": proxy_state.get_node_count(),
    }


@router.get("/ip-check")
async def ip_check():
    """
    检查当前代理的出口 IP 和直连 IP。
    可以通过比较两个 IP 判断代理是否真的在换 IP。
    """
    import asyncio
    import httpx

    IP_APIS = [
        "https://api.ipify.org?format=json",
        "https://ip.sb/geoip",
        "https://httpbin.org/ip",
    ]

    async def _fetch_ip(use_proxy: bool) -> dict:
        proxy_url = proxy_state.get_proxy() if use_proxy else None
        for api in IP_APIS:
            try:
                kwargs: dict = {"timeout": 10.0}
                if proxy_url:
                    kwargs["proxy"] = proxy_url
                async with httpx.AsyncClient(**kwargs) as client:
                    resp = await client.get(api)
                    if resp.status_code == 200:
                        data = resp.json()
                        ip = data.get("ip") or data.get("origin", "").split(",")[0].strip()
                        return {"ip": ip, "source": api, "ok": True}
            except Exception as e:
                continue
        return {"ip": None, "ok": False, "error": "所有 IP 检查接口均失败"}

    proxy_url = proxy_state.get_proxy()
    direct_task = asyncio.create_task(_fetch_ip(False))
    proxy_task = asyncio.create_task(_fetch_ip(True)) if proxy_url else None

    direct_result = await direct_task
    proxy_result = await proxy_task if proxy_task else {"ip": None, "ok": False, "error": "无代理配置"}

    same_ip = (
        direct_result.get("ip") and
        proxy_result.get("ip") and
        direct_result["ip"] == proxy_result["ip"]
    )

    return {
        "direct_ip": direct_result,
        "proxy_ip": proxy_result,
        "same_as_direct": same_ip,
        "warning": "代理出口 IP 与直连相同，换节点不会改变 Google 看到的 IP" if same_ip else None,
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
        <button class="btn btn-ghost" onclick="checkIP(this)">🌐 查看出口 IP</button>
        <button class="btn btn-danger" onclick="clearProxy()">✕ 清除代理</button>
      </div>
    </div>
    <div id="ip-result" style="display:none;margin-top:14px;padding:12px 14px;background:#0f172a;border-radius:8px;font-size:13px;font-family:monospace;line-height:1.8;"></div>
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

  <!-- 订阅链接管理 -->
  <div class="card">
    <div class="card-title">
      <span>🔗 订阅链接管理</span>
      <button class="btn btn-ghost btn-sm" onclick="loadSubUrls()">刷新</button>
    </div>
    <div id="sub-alert" class="alert"></div>
    <div id="sub-url-list" style="margin-bottom:12px"><span class="loading">加载中…</span></div>
    <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap">
      <input class="input" id="sub-url-input" placeholder="粘贴订阅链接，如 https://example.com/sub?token=xxx" style="flex:1;min-width:260px">
      <button class="btn btn-success" onclick="addSubUrl()">＋ 添加</button>
    </div>
    <div class="hint" style="margin-top:8px">支持多条订阅链接，刷新节点时自动合并所有链接的节点</div>
  </div>

  <!-- 订阅节点列表 -->
  <div class="card">
    <div class="card-title">订阅节点列表</div>
    <div id="alert-box" class="alert"></div>
    <div class="btn-row" style="margin-bottom:14px">
      <button class="btn btn-ghost" onclick="loadNodes(true)" id="btn-refresh">🔄 刷新节点列表</button>
      <button class="btn btn-primary" onclick="startBench(this)" id="btn-bench">⚡ 一键测速排序</button>
      <button class="btn btn-success" onclick="pickBest()" id="btn-best" style="display:none">🏆 选最优节点</button>
      <button class="btn btn-ghost" onclick="startQuotaScan()" id="btn-quota">🔍 检测可用节点</button>
      <button class="btn" onclick="removeFailedNodes()" id="btn-remove-failed" style="display:none;background:#dc2626;color:#fff">🗑 删除无效节点</button>
      <span class="lm" id="node-count"></span>
      <span id="detect-status" style="font-size:12px;color:#f59e0b;display:none">⏳ 正在检测出口国家…</span>
      <span id="bench-status" style="font-size:12px;color:#f59e0b;display:none">⏳ 测速中…</span>
      <span id="quota-status" style="font-size:12px;color:#f59e0b;display:none">⏳ 正在检测节点可用性…</span>
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

async function checkIP(btn) {
  const box = document.getElementById('ip-result');
  box.style.display = 'block';
  box.innerHTML = '<span style="color:#64748b">正在检查出口 IP，最多等 15 秒…</span>';
  btn.disabled = true;
  try {
    const d = await api('/ip-check');
    const directIP  = d.direct_ip?.ip  || '获取失败';
    const proxyIP   = d.proxy_ip?.ip   || '获取失败';
    const sameColor = d.same_as_direct ? '#ef4444' : '#22c55e';
    const sameLabel = d.same_as_direct
      ? '⚠️  与直连相同 — 换节点不会让 Google 认为是不同 IP'
      : '✅  不同 — 代理确实换了出口 IP';
    box.innerHTML = `
      <div style="color:#94a3b8;margin-bottom:6px">📡 当前网络出口</div>
      <div><span style="color:#64748b">直连 IP：</span><span style="color:#7dd3fc">${directIP}</span></div>
      <div><span style="color:#64748b">代理 IP：</span><span style="color:#7dd3fc">${proxyIP}</span></div>
      <div style="margin-top:8px;color:${sameColor}">${sameLabel}</div>
      ${d.warning ? `<div style="margin-top:6px;color:#fbbf24;font-size:12px">💡 如果 IP 相同，说明订阅里的节点都通过同一段 IP 出口（常见于 CF 系节点），需要添加其他运营商的节点才能真正分摊额度。</div>` : ''}
    `;
  } catch(e) {
    box.innerHTML = '<span style="color:#ef4444">检查失败：' + e + '</span>';
  } finally {
    btn.disabled = false;
  }
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

    // 显示每条链接的拉取结果
    if (refresh && d.fetch_results && d.fetch_results.length) {
      const parts = d.fetch_results.map(r =>
        r.ok
          ? `✅ ${escHtml(r.url)} → ${r.count} 个节点`
          : `❌ ${escHtml(r.url)} → ${escHtml(r.error || '失败')}`
      ).join('<br>');
      const box = document.getElementById('alert-box');
      box.className = 'alert ' + (d.fetch_results.every(r => r.ok) ? 'alert-success' : 'alert-error');
      box.innerHTML = parts;
      box.style.display = 'block';
      // 有警告单独显示
      if (d.warning) {
        box.innerHTML = `⚠️ ${escHtml(d.warning)}<br><small style="color:#94a3b8">${parts}</small>`;
        box.className = 'alert alert-error';
      }
    }

    if (d.error && !d.nodes?.length) { showAlert('拉取失败: ' + d.error); return; }
    nodes = d.nodes || [];
    document.getElementById('node-count').textContent = `共 ${d.total} 个节点`;
    renderTable();
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

function latencyBadge(n) {
  const ms = n.latency_ms;
  if (ms === undefined || ms === null) return '<span class="lm" style="font-size:11px">-</span>';
  const color = ms < 100 ? '#22c55e' : ms < 300 ? '#f59e0b' : '#ef4444';
  return `<span style="color:${color};font-family:monospace;font-size:12px;font-weight:600">${ms}ms</span>`;
}

let quotaResults = {}; // index -> "ok"|"dead"|"exhausted"|"checking"

function quotaBadge(i) {
  const s = quotaResults[i];
  if (!s) return '';
  if (s === 'checking') return '<span style="color:#f59e0b;font-size:11px">⏳</span>';
  if (s === 'ok') return '<span style="color:#22c55e;font-size:12px" title="可连接Google">✅</span>';
  if (s === 'exhausted') return '<span style="color:#ef4444;font-size:12px" title="429 额度耗尽">🚫</span>';
  return '<span style="color:#6b7280;font-size:12px" title="连接超时/失败">💀</span>';
}

function renderTable() {
  if (!nodes.length) {
    document.getElementById('table-container').innerHTML = '<span class="loading">没有找到节点</span>';
    return;
  }
  const hasBench = nodes.some(n => n.latency_ms !== undefined && n.latency_ms !== null);
  const hasQuota = Object.keys(quotaResults).length > 0;
  const bestBtn = document.getElementById('btn-best');
  bestBtn.style.display = hasBench ? 'inline-flex' : 'none';

  const quotaHeader = hasQuota ? '<th>可用</th>' : '';
  let html = `<table><thead><tr><th>#</th><th>延迟</th><th>国家</th><th>名称</th><th>地址</th><th>端口</th><th>协议</th>${quotaHeader}<th>操作</th></tr></thead><tbody>`;
  nodes.forEach((n, i) => {
    const protoCls = n.protocol === 'vless' ? 'proto-vless' : 'proto-vmess';
    const isBest = hasBench && i === 0 && n.latency_ms !== null;
    const qStatus = quotaResults[i];
    const isDead = qStatus === 'dead' || qStatus === 'exhausted';
    const rowStyle = isDead ? 'opacity:0.45;' : isBest ? 'background:#14532d22;' : i === 1 && hasBench && n.latency_ms !== null ? 'background:#0c4a6e22;' : '';
    const crown = isBest ? '👑 ' : '';
    const quotaCell = hasQuota ? `<td style="text-align:center">${quotaBadge(i)}</td>` : '';
    html += `<tr style="${rowStyle}">
      <td class="lm">${i+1}</td>
      <td>${latencyBadge(n)}</td>
      <td>${countryBadge(n)}</td>
      <td>${crown}${escHtml(n.name||'')}</td>
      <td style="font-family:monospace;font-size:12px">${escHtml((n.server||n.address||''))}</td>
      <td>${n.port}</td>
      <td><span class="proto-badge ${protoCls}">${(n.protocol||'').toUpperCase()}</span></td>
      ${quotaCell}
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

// ── 订阅链接管理 ──────────────────────────────────────────────────────────────

async function loadSubUrls() {
  try {
    const d = await api('/sub-urls');
    const urls = d.urls || [];
    const el = document.getElementById('sub-url-list');
    if (!urls.length) {
      el.innerHTML = '<div class="empty-state">还没有订阅链接</div>';
      return;
    }
    el.innerHTML = urls.map((u, i) => `
      <div style="display:flex;align-items:center;gap:10px;padding:8px 0;border-bottom:1px solid #1e3a5f;flex-wrap:wrap">
        <span style="color:#64748b;font-size:12px;min-width:20px">${i+1}</span>
        <span style="font-family:monospace;font-size:12px;color:#7dd3fc;flex:1;word-break:break-all">${escHtml(u)}</span>
        <button class="btn btn-warn btn-sm" onclick="deleteSubUrl(${i})">删除</button>
      </div>
    `).join('');
  } catch(e) { console.error(e); }
}

async function addSubUrl() {
  const url = document.getElementById('sub-url-input').value.trim();
  if (!url) { showAlert('请粘贴订阅链接', 'error', 'sub-alert'); return; }
  const d = await api('/sub-urls', { method: 'POST', body: JSON.stringify({ url }) });
  if (d.ok) {
    showAlert('✅ 已添加订阅链接', 'success', 'sub-alert');
    document.getElementById('sub-url-input').value = '';
    await loadSubUrls();
  } else {
    showAlert('添加失败: ' + d.error, 'error', 'sub-alert');
  }
}

async function deleteSubUrl(i) {
  if (!confirm('确认删除该订阅链接？')) return;
  const d = await api(`/sub-urls/${i}`, { method: 'DELETE' });
  if (d.ok) {
    showAlert('已删除', 'success', 'sub-alert');
    await loadSubUrls();
  } else {
    showAlert('删除失败: ' + d.error, 'error', 'sub-alert');
  }
}

// ── 测速 ──────────────────────────────────────────────────────────────────────

let benchPollTimer = null;

async function startBench(btn) {
  if (!nodes.length) { showAlert('请先刷新节点列表', 'error'); return; }
  btn.disabled = true; btn.textContent = '测速中…';
  const bsEl = document.getElementById('bench-status');
  bsEl.style.display = 'inline'; bsEl.style.color = '#f59e0b';
  bsEl.textContent = `⏳ 正在测速 ${nodes.length} 个节点…`;
  try {
    const d = await api('/bench', { method: 'POST', body: '{}' });
    if (!d.ok) { showAlert('测速失败: ' + d.error); return; }
    if (benchPollTimer) clearInterval(benchPollTimer);
    benchPollTimer = setInterval(pollBenchStatus, 1500);
  } catch(e) {
    showAlert('测速请求失败: ' + e);
  } finally {
    btn.disabled = false; btn.textContent = '⚡ 一键测速排序';
  }
}

async function pollBenchStatus() {
  try {
    const d = await api('/bench-status');
    const bsEl = document.getElementById('bench-status');
    if (!d.running) {
      clearInterval(benchPollTimer); benchPollTimer = null;
      bsEl.style.color = '#22c55e';
      bsEl.textContent = '✅ 测速完成，已按延迟排序';
      await loadNodes();
    }
  } catch(e) {}
}

async function pickBest() {
  if (!nodes.length) return;
  const best = nodes.find(n => n.latency_ms !== null && n.latency_ms !== undefined);
  if (!best) { showAlert('没有可用节点（全部超时）', 'error'); return; }
  const idx = nodes.indexOf(best);
  showAlert(`正在选择最优节点: 👑 ${best.name} (${best.latency_ms}ms)…`, 'success');
  await selectNode(idx);
}

// ── 额度检测 ──────────────────────────────────────────────────────────────────

let quotaPollTimer = null;

async function startQuotaScan() {
  if (!nodes.length) { showAlert('请先加载节点列表', 'error'); return; }
  quotaResults = {};
  document.getElementById('btn-quota').disabled = true;
  document.getElementById('btn-remove-failed').style.display = 'none';
  const qs = document.getElementById('quota-status');
  qs.style.display = 'inline';
  qs.style.color = '#f59e0b';
  qs.textContent = '⏳ 正在检测节点可用性…';
  renderTable();
  try {
    const d = await api('/quota-scan', { method: 'POST', body: JSON.stringify({ max_nodes: Math.min(nodes.length, 30) }) });
    if (!d.ok) {
      showAlert('检测失败: ' + d.error, 'error');
      qs.style.display = 'none';
      document.getElementById('btn-quota').disabled = false;
      return;
    }
    showAlert(d.message, 'success');
    if (quotaPollTimer) clearInterval(quotaPollTimer);
    quotaPollTimer = setInterval(pollQuotaStatus, 2000);
  } catch(e) {
    showAlert('启动检测出错: ' + e, 'error');
    qs.style.display = 'none';
    document.getElementById('btn-quota').disabled = false;
  }
}

async function pollQuotaStatus() {
  try {
    const d = await api('/quota-scan/status');
    quotaResults = d.results || {};
    const qs = document.getElementById('quota-status');
    renderTable();
    if (!d.running) {
      clearInterval(quotaPollTimer); quotaPollTimer = null;
      const ok = Object.values(quotaResults).filter(s => s === 'ok').length;
      const bad = Object.values(quotaResults).filter(s => s === 'dead' || s === 'exhausted').length;
      qs.style.color = '#22c55e';
      qs.textContent = `✅ 检测完成：${ok} 可用，${bad} 无效`;
      document.getElementById('btn-quota').disabled = false;
      if (bad > 0) document.getElementById('btn-remove-failed').style.display = 'inline-flex';
    } else if (d.current >= 0) {
      qs.textContent = `⏳ 检测中… (${d.current + 1}/${Object.keys(quotaResults).length || '?'})`;
    }
  } catch(e) {}
}

async function removeFailedNodes() {
  if (!confirm('确认删除所有检测结果为"超时/失败/额度耗尽"的节点？')) return;
  try {
    const d = await api('/quota-scan/remove-failed', { method: 'POST' });
    if (d.ok) {
      showAlert(`✅ ${d.message}`, 'success');
      quotaResults = {};
      document.getElementById('btn-remove-failed').style.display = 'none';
      await loadNodes();
    } else {
      showAlert('删除失败: ' + d.error, 'error');
    }
  } catch(e) {
    showAlert('操作出错: ' + e, 'error');
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
loadSubUrls();
loadLogs();
loadSettings();
setInterval(loadStatus, 8000);
setInterval(loadLogs, 15000);
</script>
</body>
</html>"""
