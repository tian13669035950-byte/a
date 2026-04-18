"""
Vertex AI 网络客户端

负责处理底层的 HTTP 请求、连接池管理和重试逻辑。
包含 Google Recaptcha Token 现抓现用逻辑。
使用 primp (Rust 静态链接 BoringSSL) 进行 Chrome TLS 指纹伪装。

代理策略：
  - recaptcha 请求：代理优先（若可用），失败降级直连
  - Vertex AI 请求：直连优先（cloudconsole-pa.clients6.google.com），
                    直连失败才走代理（适用于网络受限环境）
"""

import re
import random
import socket
import contextvars
from urllib.parse import parse_qs, urlparse
from bs4 import BeautifulSoup
from typing import Any, AsyncGenerator, Optional
import primp
import httpx
from src.core.config import load_config
from src.utils.logger import get_logger

logger = get_logger(__name__)

# 兜底直连开关（per-request 上下文变量）：
# 当所有代理节点都失败/耗尽配额时，调用方可在最后一次重试前
# 设置为 True，让本次请求直接走 Replit 出口（牺牲 Replit IP 配额
# 换取一个能给用户的回答），避免直接 429 失败。
force_direct_var: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "vertex_force_direct", default=False
)

RECAPTCHA_ANCHOR = (
    "https://www.google.com/recaptcha/enterprise/anchor"
    "?ar=1&k=6LdCjtspAAAAAMcV4TGdWLJqRTEk1TfpdLqEnKdj"
    "&co=aHR0cHM6Ly9jb25zb2xlLmNsb3VkLmdvb2dsZS5jb206NDQz"
    "&hl=zh-CN&v=jdMmXeCQEkPbnFDy9T04NbgJ"
    "&size=invisible&anchor-ms=20000&execute-ms=15000"
)
RECAPTCHA_RELOAD = (
    "https://www.google.com/recaptcha/enterprise/reload"
    "?k=6LdCjtspAAAAAMcV4TGdWLJqRTEk1TfpdLqEnKdj"
)


def _random_string(length: int) -> str:
    return "".join(random.choice("abcdefghijklmnopqrstuvwxyz0123456789") for _ in range(length))


def _get_proxy() -> Optional[str]:
    try:
        from proxy_manager.proxy_state import get_proxy
        return get_proxy()
    except Exception:
        return None


def _rotate_proxy() -> Optional[str]:
    """同步轮换到下一个节点，返回新的代理地址；失败返回 None"""
    try:
        from proxy_manager import proxy_state as _ps
        if _ps.get_node_count() > 1 and _ps.rotate_to_next():
            return _ps.get_proxy()
    except Exception:
        pass
    return None


def _socks5_reachable(host: str = "127.0.0.1", port: int = 1080, timeout: float = 1.0) -> bool:
    """快速检查 SOCKS5 端口是否监听中"""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _build_async_client(proxy: Optional[str] = None, timeout: int = 60) -> primp.AsyncClient:
    """创建带随机 Chrome TLS 指纹的异步客户端"""
    kwargs: dict[str, Any] = {
        "impersonate": "random",
        "verify": True,
        "follow_redirects": True,
        "timeout": timeout,
    }
    if proxy:
        kwargs["proxy"] = proxy
    return primp.AsyncClient(**kwargs)


async def _do_recaptcha_request(proxy: Optional[str]) -> Optional[str]:
    """
    执行一次 Recaptcha Token 获取（使用给定的 proxy 或直连）。
    成功返回 token，失败返回 None。
    """
    random_cb = _random_string(10)
    anchor_url = RECAPTCHA_ANCHOR + f"&cb={random_cb}"

    try:
        async with _build_async_client(proxy=proxy, timeout=20) as client:
            anchor_response = await client.get(anchor_url, timeout=15)
            if anchor_response.status_code != 200:
                logger.debug(f"anchor HTTP {anchor_response.status_code}, proxy={'有' if proxy else '无'}")
                return None

            soup = BeautifulSoup(anchor_response.text, "html.parser")
            token_element = soup.find("input", {"id": "recaptcha-token"})
            if token_element is None:
                logger.debug(f"anchor 未找到 token 元素, proxy={'有' if proxy else '无'}")
                return None

            base_token = str(token_element.get("value"))
            parsed = urlparse(anchor_url)
            params = parse_qs(parsed.query)
            payload = {
                "v": params["v"][0], "reason": "q", "k": params["k"][0],
                "c": base_token, "co": params["co"][0],
                "hl": params["hl"][0], "size": "invisible",
                "vh": "6581054572", "chr": "", "bg": "",
            }

            reload_response = await client.post(
                RECAPTCHA_RELOAD, data=payload,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=15
            )
            match = re.search(r'rresp","(.*?)"', reload_response.text)
            if not match:
                logger.debug(f"未找到 rresp, proxy={'有' if proxy else '无'}")
                return None

            return match.group(1)
    except Exception as e:
        logger.debug(f"recaptcha 异常 proxy={'有' if proxy else '无'}: {type(e).__name__}: {e}")
        return None


class FakeResponse:
    """primp AsyncResponse 封装，保持与调用方一致的接口"""

    def __init__(self, resp: primp.AsyncResponse):
        self._resp = resp
        self.status_code = resp.status_code

    @property
    def text(self) -> str:
        return self._resp.text

    async def aread(self) -> bytes:
        return self._resp.content

    async def aiter_lines(self):
        async for line in self._resp.aiter_lines():
            yield line.encode("utf-8") if isinstance(line, str) else line

    def json(self):
        import json as _json
        return _json.loads(self._resp.text)


class HttpxStreamingFakeResponse:
    """httpx 真流式响应封装，aiter_lines() 边收边发，不缓冲整体"""

    def __init__(self, resp: httpx.Response):
        self._resp = resp
        self.status_code = resp.status_code

    @property
    def text(self) -> str:
        return self._resp.text

    async def aread(self) -> bytes:
        return await self._resp.aread()

    async def aiter_lines(self):
        async for line in self._resp.aiter_lines():
            yield line.encode("utf-8") if isinstance(line, str) else line

    def json(self):
        import json as _json
        # token_counter 用法是非流式 POST 后立刻调 .json()，此时 _resp 已读完
        try:
            return self._resp.json()
        except Exception:
            return _json.loads(self._resp.text)


class MockSession:
    """primp 模式下的占位 session，兼容旧接口中的 session.close() 和 async with 调用"""

    async def close(self):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class NetworkClient:
    """底层网络客户端（使用 primp 实现 Chrome TLS 指纹伪装）"""

    def __init__(self):
        self.config = load_config()
        logger.debug("NetworkClient 初始化完成 (primp)")

    async def close(self):
        pass

    async def fetch_recaptcha_token(self, session: Any) -> Optional[str]:
        """
        获取 Google Recaptcha Token。
        策略：代理优先，与后续 Gemini 请求保持同源 IP，避免 Google 反欺诈
        将"换 IP 用 token"识别为可疑请求并返回空响应。
        直连只在没有代理或代理彻底失败时作为兜底。
        """
        # 兜底直连模式：跳过代理，直接走 Replit 出口
        if force_direct_var.get():
            logger.warning("兜底直连模式：Recaptcha 不走代理")
            for attempt in range(3):
                token = await _do_recaptcha_request(None)
                if token:
                    return token
                logger.debug(f"兜底直连 recaptcha 失败 ({attempt+1}/3)")
            logger.error("兜底直连 Recaptcha Token 获取失败")
            return None

        proxy = _get_proxy() if _socks5_reachable() else None

        # 严格代理模式：有代理时只走代理；代理全失败才允许直连（仅在没配置任何代理时）
        if proxy:
            for attempt in range(3):
                token = await _do_recaptcha_request(proxy)
                if token:
                    logger.debug("代理模式获取 Recaptcha Token 成功")
                    return token
                logger.warning(f"代理模式 recaptcha 失败 ({attempt+1}/3)")
                # 失败一次就换节点继续试（同 IP 一致性原则）
                new_proxy = _rotate_proxy()
                if new_proxy:
                    proxy = new_proxy
            logger.error("Recaptcha Token 全代理尝试均失败，已轮换节点仍失败")
            return None

        # 没有代理才走直连
        for attempt in range(3):
            token = await _do_recaptcha_request(None)
            if token:
                return token
            logger.debug(f"直连 recaptcha 失败 ({attempt+1}/3)")
        logger.error("Recaptcha Token 获取失败")
        return None

    def create_session(self) -> MockSession:
        """返回占位符 session（primp 模式每次请求独立创建客户端）"""
        return MockSession()

    async def post_request(self, session: Any, url: str, headers: dict[str, str], json_data: dict[str, Any]) -> FakeResponse:
        """
        发送非流式 POST 请求。
        策略：代理优先（不同 IP 分摊配额），失败降级直连。
        """
        # 兜底直连模式：直接走 Replit 出口
        if force_direct_var.get():
            logger.warning("兜底直连模式：POST 请求绕过代理")
            try:
                async with httpx.AsyncClient(timeout=httpx.Timeout(45.0), verify=True) as client:
                    resp = await client.post(url, headers=headers, json=json_data)
                    return HttpxStreamingFakeResponse(resp)
            except Exception as e:
                logger.warning(f"兜底直连 httpx POST 失败，降级到 primp: {e}")
                async with _build_async_client(proxy=None) as client:
                    resp = await client.post(url, headers=headers, json=json_data, timeout=30)
                    return FakeResponse(resp)

        proxy = _get_proxy() if _socks5_reachable() else None

        # 严格代理模式：配置了代理就只走代理（不烧 Replit IP）
        if proxy:
            last_exc: Optional[Exception] = None
            for attempt in range(3):
                try:
                    async with httpx.AsyncClient(proxy=proxy, timeout=httpx.Timeout(45.0), verify=True) as client:
                        resp = await client.post(url, headers=headers, json=json_data)
                        logger.debug(f"httpx 代理 POST 成功, status={resp.status_code}")
                        return HttpxStreamingFakeResponse(resp)
                except Exception as e:
                    last_exc = e
                    logger.warning(f"httpx 代理 POST 失败 ({attempt+1}/3): {type(e).__name__}: {e}")
                    new_proxy = _rotate_proxy()
                    if new_proxy:
                        proxy = new_proxy
                        logger.info("已切换到下一个代理节点")
                    else:
                        break
            logger.error(f"所有代理 POST 失败，未降级直连以保护 IP 配额")
            raise last_exc if last_exc else RuntimeError("所有代理节点均失败")

        # 完全没配置代理时才允许直连
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(45.0), verify=True) as client:
                resp = await client.post(url, headers=headers, json=json_data)
                return HttpxStreamingFakeResponse(resp)
        except Exception as e:
            async with _build_async_client(proxy=None) as client:
                resp = await client.post(url, headers=headers, json=json_data, timeout=30)
                return FakeResponse(resp)

    async def stream_request(self, session: Any, method: str, url: str, headers: dict[str, str], json_data: dict[str, Any]) -> AsyncGenerator[FakeResponse, None]:
        """
        发送流式请求。
        策略：代理优先（不同 IP 分摊配额），失败才直连。
        httpx 支持边收边发（真正流式），primp 会等全部接收完才返回。
        """
        # 兜底直连模式：直接走 Replit 出口
        if force_direct_var.get():
            logger.warning("兜底直连模式：stream 请求绕过代理")
            try:
                async with httpx.AsyncClient(timeout=httpx.Timeout(60.0), verify=True) as client:
                    async with client.stream(method, url, headers=headers, json=json_data) as resp:
                        yield HttpxStreamingFakeResponse(resp)
                        return
            except Exception as e:
                logger.warning(f"兜底直连 httpx stream 失败，降级到 primp: {e}")
                async with _build_async_client(proxy=None) as client:
                    resp = await client.request(method, url, headers=headers, json=json_data, timeout=60)
                    yield FakeResponse(resp)
                    return

        proxy = _get_proxy() if _socks5_reachable() else None
        last_exc: Optional[Exception] = None

        # 1. httpx + 代理（不同 IP，配额独立，真正流式）
        # 代理连接失败时自动换下一个节点再试，而不是立刻降级直连
        proxy_attempts = 0
        while proxy and proxy_attempts < 3:
            proxy_attempts += 1
            try:
                async with httpx.AsyncClient(proxy=proxy, timeout=httpx.Timeout(60.0), verify=True) as client:
                    async with client.stream(method, url, headers=headers, json=json_data) as resp:
                        logger.debug(f"httpx 代理 stream 成功, status={resp.status_code}")
                        yield HttpxStreamingFakeResponse(resp)
                        return
            except Exception as e:
                err_str = str(e)
                if "timed out" in err_str.lower() or "timeout" in err_str.lower():
                    from src.core.errors import InternalError
                    logger.warning(f"代理 stream 超时，将触发重试: {e}")
                    raise InternalError(message="Upstream request timed out, retrying...")
                logger.warning(f"httpx 代理 stream 失败（第{proxy_attempts}次），尝试换节点: {type(e).__name__}: {e}")
                last_exc = e
                new_proxy = _rotate_proxy()
                if new_proxy:
                    proxy = new_proxy
                    logger.info(f"已切换到新代理节点，继续重试")
                else:
                    break  # 无更多节点可用
        # 严格代理模式：有代理时绝不降级直连
        if proxy_attempts > 0:
            logger.error("所有代理 stream 失败，未降级直连以保护 IP 配额")
            if last_exc:
                raise last_exc
            from src.core.errors import InternalError
            raise InternalError(message="所有代理节点均不可用，请检查节点状态")

        # 完全没配置代理时才允许直连
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(60.0), verify=True) as client:
                async with client.stream(method, url, headers=headers, json=json_data) as resp:
                    yield HttpxStreamingFakeResponse(resp)
                    return
        except Exception as e:
            async with _build_async_client(proxy=None) as client:
                resp = await client.request(method, url, headers=headers, json=json_data, timeout=60)
                yield FakeResponse(resp)
                return
