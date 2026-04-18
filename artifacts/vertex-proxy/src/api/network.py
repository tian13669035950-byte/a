"""
Vertex AI 网络客户端

负责处理底层的 HTTP 请求、连接池管理和重试逻辑。
包含 Google Recaptcha Token 现抓现用逻辑。
使用 primp (Rust 静态链接 BoringSSL) 进行 Chrome TLS 指纹伪装。

代理策略（默认严格模式）：
  - 所有出网（recaptcha + Vertex）一律必须走 SOCKS5 代理
  - 代理失败只换节点重试，绝不降级直连（避免暴露 Replit IP）
  - 设置环境变量 STRICT_PROXY=0 才允许降级直连（仅本地开发用）
"""

import os
import re
import random
import socket
from urllib.parse import parse_qs, urlparse
from bs4 import BeautifulSoup
from typing import Any, AsyncGenerator, Optional
import primp
import httpx
from src.core.config import load_config
from src.core.errors import InternalError
from src.utils.logger import get_logger

logger = get_logger(__name__)

STRICT_PROXY = os.environ.get("STRICT_PROXY", "1") != "0"

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
    """primp 模式下的占位 session，兼容旧接口中的 session.close() 和 async with 调用"""

    async def close(self):
        pass

    async def aclose(self):
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
        严格模式（默认）：必须走代理。每失败一次轮换节点，最多 5 次。
        宽松模式（STRICT_PROXY=0）：代理失败后才降级直连。
        """
        proxy = _get_proxy() if _socks5_reachable() else None

        # 1. 代理优先（每次失败轮换节点）
        if proxy:
            current_proxy = proxy
            for attempt in range(5):
                token = await _do_recaptcha_request(current_proxy)
                if token:
                    logger.info(f"[Proxy] recaptcha 通过代理获取成功 (尝试 {attempt+1})")
                    return token
                logger.warning(f"[Proxy] recaptcha 代理失败 ({attempt+1}/5)，轮换节点")
                new_p = _rotate_proxy()
                if new_p:
                    current_proxy = new_p
                else:
                    break
        elif STRICT_PROXY:
            logger.error("[STRICT] SOCKS5 代理不可用，拒绝直连 google.com 抓 recaptcha")
            return None

        # 2. 严格模式禁止降级直连
        if STRICT_PROXY:
            logger.error("[STRICT] 所有代理节点 recaptcha 均失败，不降级直连（避免暴露 Replit IP）")
            return None

        # 3. 宽松模式才允许直连兜底
        logger.warning("[Loose] 代理 recaptcha 失败，降级直连")
        for attempt in range(3):
            token = await _do_recaptcha_request(None)
            if token:
                logger.info("[Loose] recaptcha 直连获取成功")
                return token
            logger.debug(f"直连 recaptcha 失败 ({attempt+1}/3)")

        logger.error("Recaptcha Token 获取失败（直连+代理均已尝试）")
        return None

    def create_session(self) -> MockSession:
        """返回占位符 session（primp 模式每次请求独立创建客户端）"""
        return MockSession()

    async def post_request(self, session: Any, url: str, headers: dict[str, str], json_data: dict[str, Any]) -> FakeResponse:
        """
        发送非流式 POST 请求。
        严格模式（默认）：必须走代理，失败只换节点重试，绝不降级直连。
        """
        proxy = _get_proxy() if _socks5_reachable() else None

        # 1. httpx + 代理（每次失败轮换节点，最多 5 次）
        if proxy:
            current_proxy = proxy
            last_exc: Optional[Exception] = None
            for attempt in range(5):
                try:
                    async with httpx.AsyncClient(proxy=current_proxy, timeout=httpx.Timeout(45.0), verify=True) as client:
                        resp = await client.post(url, headers=headers, json=json_data)
                        logger.debug(f"httpx 代理 POST 成功, status={resp.status_code} (节点尝试 {attempt+1})")
                        return HttpxStreamingFakeResponse(resp)
                except Exception as e:
                    last_exc = e
                    logger.warning(f"httpx 代理 POST 失败 ({attempt+1}/5)，轮换节点: {type(e).__name__}: {e}")
                    new_p = _rotate_proxy()
                    if new_p:
                        current_proxy = new_p
                    else:
                        break
        elif STRICT_PROXY:
            logger.error("严格模式：SOCKS5 不可用，拒绝直连 Vertex（避免暴露 Replit IP）")
            raise InternalError(message="Proxy unavailable in STRICT_PROXY mode; refusing to leak Replit IP")

        # 严格模式禁止降级直连
        if STRICT_PROXY:
            logger.error("严格模式：所有代理节点 POST 均失败，不降级直连")
            if proxy:
                raise InternalError(message="All proxy nodes failed; STRICT_PROXY blocks direct fallback")
            raise InternalError(message="No proxy available; STRICT_PROXY blocks direct fallback")

        # 2. httpx 直连降级（仅宽松模式）
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(45.0), verify=True) as client:
                resp = await client.post(url, headers=headers, json=json_data)
                logger.debug(f"[宽松模式] httpx 直连 POST 成功, status={resp.status_code}")
                return HttpxStreamingFakeResponse(resp)
        except Exception as e:
            logger.warning(f"[宽松模式] httpx 直连 POST 失败，降级 primp: {type(e).__name__}: {e}")

        # 3. primp 直连兜底（仅宽松模式）
        try:
            async with _build_async_client(proxy=None) as client:
                resp = await client.post(url, headers=headers, json=json_data, timeout=30)
                return FakeResponse(resp)
        except Exception as e:
            logger.error(f"所有方式 POST 均失败: {type(e).__name__}: {e}")
            raise

    async def stream_request(self, session: Any, method: str, url: str, headers: dict[str, str], json_data: dict[str, Any]) -> AsyncGenerator[FakeResponse, None]:
        """
        发送流式请求。
        严格模式（默认）：必须走代理，失败只换节点重试，绝不降级直连。
        """
        proxy = _get_proxy() if _socks5_reachable() else None
        last_exc: Optional[Exception] = None

        # 1. httpx + 代理（每次失败轮换节点，最多 5 次）
        proxy_attempts = 0
        max_proxy_attempts = 5
        while proxy and proxy_attempts < max_proxy_attempts:
            proxy_attempts += 1
            try:
                async with httpx.AsyncClient(proxy=proxy, timeout=httpx.Timeout(60.0), verify=True) as client:
                    async with client.stream(method, url, headers=headers, json=json_data) as resp:
                        logger.debug(f"httpx 代理 stream 成功, status={resp.status_code} (节点尝试 {proxy_attempts})")
                        yield HttpxStreamingFakeResponse(resp)
                        return
            except Exception as e:
                err_str = str(e)
                if "timed out" in err_str.lower() or "timeout" in err_str.lower():
                    logger.warning(f"代理 stream 超时，将触发重试: {e}")
                    raise InternalError(message="Upstream request timed out, retrying...")
                logger.warning(f"httpx 代理 stream 失败（第{proxy_attempts}次），尝试换节点: {type(e).__name__}: {e}")
                last_exc = e
                new_proxy = _rotate_proxy()
                if new_proxy:
                    proxy = new_proxy
                    logger.info(f"已切换到新代理节点，继续重试 ({proxy_attempts}/{max_proxy_attempts})")
                else:
                    break

        if not proxy and STRICT_PROXY:
            logger.error("严格模式：SOCKS5 不可用，拒绝直连 Vertex stream")
            raise InternalError(message="Proxy unavailable in STRICT_PROXY mode; refusing to leak Replit IP")

        # 严格模式禁止降级直连
        if STRICT_PROXY:
            logger.error("严格模式：所有代理节点 stream 均失败，不降级直连")
            if last_exc:
                raise last_exc
            raise InternalError(message="All proxy nodes failed; STRICT_PROXY blocks direct fallback")

        # 2. httpx 直连降级（仅宽松模式）
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(60.0), verify=True) as client:
                async with client.stream(method, url, headers=headers, json=json_data) as resp:
                    logger.debug(f"[宽松模式] httpx 直连 stream 成功, status={resp.status_code}")
                    yield HttpxStreamingFakeResponse(resp)
                    return
        except Exception as e:
            err_str = str(e)
            if "timed out" in err_str.lower() or "timeout" in err_str.lower():
                logger.warning(f"[宽松模式] httpx 直连超时，将触发重试: {e}")
                raise InternalError(message="Upstream request timed out, retrying...")
            logger.warning(f"[宽松模式] httpx 直连 stream 失败，降级 primp: {type(e).__name__}: {e}")
            last_exc = e

        # 3. primp 直连兜底（仅宽松模式）
        try:
            async with _build_async_client(proxy=None) as client:
                resp = await client.request(method, url, headers=headers, json=json_data, timeout=60)
                logger.debug("[宽松模式] primp 直连兜底成功（非真流式）")
                yield FakeResponse(resp)
                return
        except Exception as e:
            err_str = str(e)
            if "timed out" in err_str.lower() or "timeout" in err_str.lower():
                logger.warning(f"[宽松模式] primp 直连超时，将触发重试: {e}")
                raise InternalError(message="Upstream request timed out, retrying...")
            logger.error(f"所有方式 stream 均失败: {type(e).__name__}: {e}")
            if last_exc:
                raise last_exc
            raise
