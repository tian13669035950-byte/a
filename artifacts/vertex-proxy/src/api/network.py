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
from urllib.parse import parse_qs, urlparse
from bs4 import BeautifulSoup
from typing import Any, AsyncGenerator, Optional
import primp
import httpx
from src.core.config import load_config
from src.utils.logger import get_logger

logger = get_logger(__name__)

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


class MockSession:
    """primp 模式下的占位 session，兼容旧接口中的 session.close() 调用"""

    async def close(self):
        pass


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
        策略：
          1. 若代理可用 → 优先走代理（绕过IP检测）
          2. 代理不可用/失败 → 直连（primp Chrome指纹仍能通过检测）
        每种方式最多重试 3 次。
        """
        proxy = _get_proxy()
        proxy_alive = bool(proxy) and _socks5_reachable()

        if proxy_alive:
            for attempt in range(3):
                token = await _do_recaptcha_request(proxy)
                if token:
                    logger.debug("代理模式获取 Recaptcha Token 成功")
                    return token
                logger.warning(f"代理模式 recaptcha 失败 ({attempt+1}/3)")
            logger.warning("代理模式全部失败，降级直连")

        for attempt in range(3):
            token = await _do_recaptcha_request(None)
            if token:
                logger.debug("直连模式获取 Recaptcha Token 成功")
                return token
            logger.warning(f"直连模式 recaptcha 失败 ({attempt+1}/3)")

        logger.error("Recaptcha Token 获取失败（代理+直连均已尝试）")
        return None

    def create_session(self) -> MockSession:
        """返回占位符 session（primp 模式每次请求独立创建客户端）"""
        return MockSession()

    async def post_request(self, session: Any, url: str, headers: dict[str, str], json_data: dict[str, Any]) -> FakeResponse:
        """
        发送非流式 POST 请求。
        策略：直连优先，失败才走代理。
        （cloudconsole-pa.clients6.google.com 从生产服务器可直连，但CF socks5代理无法到达）
        """
        # 1. 直连尝试
        try:
            async with _build_async_client(proxy=None) as client:
                resp = await client.post(url, headers=headers, json=json_data, timeout=30)
                return FakeResponse(resp)
        except Exception as e:
            logger.warning(f"直连 POST 失败: {type(e).__name__}: {e}")

        # 2. 代理降级
        proxy = _get_proxy() if _socks5_reachable() else None
        if proxy:
            try:
                async with _build_async_client(proxy=proxy) as client:
                    resp = await client.post(url, headers=headers, json=json_data, timeout=30)
                    return FakeResponse(resp)
            except Exception as e:
                logger.error(f"代理 POST 也失败: {type(e).__name__}: {e}")
                raise
        else:
            raise ConnectionError(f"直连失败且无可用代理: {url}")

    async def stream_request(self, session: Any, method: str, url: str, headers: dict[str, str], json_data: dict[str, Any]) -> AsyncGenerator[FakeResponse, None]:
        """
        发送流式请求。
        策略：httpx 直连真流式优先，失败降级到 primp（再失败走 primp 代理）。
        httpx 支持边收边发（真正流式），primp 会等全部接收完才返回。
        """
        last_exc: Optional[Exception] = None

        # 1. httpx 直连（真正流式：边收边转发，SillyTavern 等客户端能看到逐字输出）
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(60.0), verify=True) as client:
                async with client.stream(method, url, headers=headers, json=json_data) as resp:
                    logger.debug(f"httpx 直连 stream 成功, status={resp.status_code}")
                    yield HttpxStreamingFakeResponse(resp)
                    return
        except Exception as e:
            err_str = str(e)
            if "timed out" in err_str.lower() or "timeout" in err_str.lower():
                from src.core.errors import InternalError
                logger.warning(f"httpx 直连超时，将触发重试: {e}")
                raise InternalError(message="Upstream request timed out, retrying...")
            logger.warning(f"httpx 直连 stream 失败，降级 primp: {type(e).__name__}: {e}")
            last_exc = e

        # 2. primp 直连降级（非真流式，全量缓冲后一次性发出）
        try:
            async with _build_async_client(proxy=None) as client:
                resp = await client.request(method, url, headers=headers, json=json_data, timeout=60)
                logger.debug("primp 直连降级成功（非真流式）")
                yield FakeResponse(resp)
                return
        except Exception as e:
            err_str = str(e)
            if "timed out" in err_str.lower() or "timeout" in err_str.lower():
                from src.core.errors import InternalError
                logger.warning(f"primp 直连超时，将触发重试: {e}")
                raise InternalError(message="Upstream request timed out, retrying...")
            logger.warning(f"primp 直连 stream 失败: {type(e).__name__}: {e}")
            last_exc = e

        # 3. primp 代理降级
        proxy = _get_proxy() if _socks5_reachable() else None
        if proxy:
            try:
                async with _build_async_client(proxy=proxy) as client:
                    resp = await client.request(method, url, headers=headers, json=json_data, timeout=60)
                    logger.debug("primp 代理降级成功（非真流式）")
                    yield FakeResponse(resp)
                    return
            except Exception as e:
                err_str = str(e)
                if "timed out" in err_str.lower() or "timeout" in err_str.lower():
                    from src.core.errors import InternalError
                    logger.warning(f"代理请求超时，将触发重试: {e}")
                    raise InternalError(message="Upstream request timed out, retrying...")
                logger.error(f"代理 stream 也失败: {type(e).__name__}: {e}")
                raise
        else:
            if last_exc:
                raise last_exc
            raise ConnectionError(f"所有方式均失败: {url}")
