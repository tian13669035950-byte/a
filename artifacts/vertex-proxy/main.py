"""Vertex AI Proxy 入口"""
import asyncio
import os
import random
import time
import uvicorn

from src.core import (
    load_config,
    PORT_API,
)
from src.api import VertexAIClient, create_app
from src.core.auth import api_key_manager
from src.utils.logger import get_logger, configure_logging, set_request_id
from src.api.admin import router as admin_router, ensure_admin_password
from proxy_manager.routes import router as proxy_router, restore_active_node

logger = get_logger(__name__)


async def _keepalive_loop(port: int) -> None:
    """
    保活循环：免费 Replit 容器空闲时会被休眠，本任务用随机间隔自 ping，
    顺便循环访问几个轻量端点，让事件循环 / 上游连接保持温热。
    - 间隔随机 180-420 秒（避免出现规律性流量被外部判定为机器流量）
    - 路径在小集合内轮换 + 随机 query 参数防缓存
    - 任何异常都吞掉，不影响主服务
    """
    import httpx

    paths = ["/health", "/admin/api/status", "/proxy-manager/api/status"]
    user_agents = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 KeepAlive/1.0",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 KeepAlive/1.0",
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 KeepAlive/1.0",
    ]
    base = f"http://127.0.0.1:{port}"

    # 启动后等 30 秒再开始，避开主服务初始化
    await asyncio.sleep(30)
    logger.info(f"[KeepAlive] 启动，目标 {base}，间隔 180-420 秒随机")

    while True:
        try:
            path = random.choice(paths)
            ua = random.choice(user_agents)
            ts = int(time.time())
            url = f"{base}{path}?_={ts}&r={random.randint(1000, 9999)}"
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.get(url, headers={"User-Agent": ua})
                logger.debug(f"[KeepAlive] {path} -> {r.status_code}")
        except Exception as e:
            logger.debug(f"[KeepAlive] ping 失败（忽略）: {e}")

        # 随机间隔 3-7 分钟
        await asyncio.sleep(random.randint(180, 420))


async def _auto_init_proxy() -> None:
    """
    生产环境启动时自动初始化代理节点。
    逻辑：
      1. 若已有活跃节点（restore_active_node 恢复成功）则跳过。
      2. 否则自动拉取订阅，依次尝试前 10 个节点，选出第一个可用的。
    整个过程在后台异步运行，不阻塞服务器启动。
    """
    import time
    from proxy_manager import proxy_state
    from proxy_manager.routes import SUB_URL
    from proxy_manager.subscription import fetch_and_parse
    from proxy_manager.xray_manager import start_xray, ensure_xray

    # 已恢复成功，跳过
    if proxy_state.get_proxy():
        logger.info("代理已就绪（从磁盘恢复），跳过自动初始化")
        return

    logger.info("未检测到活跃代理，开始自动初始化...")

    # 确保 xray 二进制存在
    try:
        ensure_xray()
    except Exception as e:
        logger.warning(f"xray 二进制检查失败: {e}")

    # 拉取订阅节点
    try:
        logger.info(f"正在拉取订阅节点...")
        nodes = await asyncio.to_thread(fetch_and_parse, SUB_URL)
        logger.info(f"订阅拉取成功，共 {len(nodes)} 个节点")
    except Exception as e:
        logger.error(f"拉取订阅失败: {e}")
        return

    if not nodes:
        logger.error("订阅中没有可用节点")
        return

    # 保存节点列表到内存
    proxy_state.set_nodes(nodes)

    # 依次尝试前 10 个节点，找到第一个能启动的
    for i, node in enumerate(nodes[:10]):
        try:
            logger.info(f"尝试节点 #{i}: {node.get('name', 'unknown')}")
            ok, err = await asyncio.to_thread(start_xray, node)
            if ok:
                proxy_state.set_proxy("socks5://127.0.0.1:1080")
                logger.success(f"✅ 自动选中节点 #{i}: {node.get('name', 'unknown')}")
                return
            else:
                logger.warning(f"节点 #{i} 启动失败: {err}")
        except Exception as e:
            logger.warning(f"节点 #{i} 异常: {e}")

    logger.error("所有自动尝试的节点均失败，请在管理界面手动选择节点")


async def main() -> None:
    """启动服务器"""
    set_request_id("startup")

    config = load_config()
    debug_mode = config.get("debug", False)

    port = int(os.environ.get("PORT", PORT_API))

    logger.info("=" * 60)
    logger.info("Vertex AI Proxy 启动中...")
    logger.info(f"调试模式: {'开启' if debug_mode else '关闭'}")
    logger.info(f"API 端口: {port}")

    api_key_manager.load_keys()
    ensure_admin_password()

    vertex_client = VertexAIClient()

    app = create_app(vertex_client)
    app.include_router(proxy_router)
    app.include_router(admin_router)

    logger.info("恢复上次选中的代理节点...")
    restore_active_node()

    logger.info(f"启动 HTTP API 服务器 (端口: {port})")
    uvicorn_config = uvicorn.Config(
        app,
        host="0.0.0.0",
        port=port,
        log_level="info",
        log_config=None
    )
    server = uvicorn.Server(uvicorn_config)

    logger.success("服务启动完成，系统运行中...")
    logger.info("=" * 60)

    # 启动后台自动初始化代理（不阻塞服务器）
    asyncio.create_task(_auto_init_proxy())
    # 启动保活循环（免费容器防休眠）
    asyncio.create_task(_keepalive_loop(port))

    try:
        await server.serve()
    except asyncio.CancelledError:
        logger.info("收到取消信号，开始关闭服务...")
    except KeyboardInterrupt:
        logger.info("收到中断信号 (Ctrl+C)，开始关闭服务...")
    finally:
        logger.info("开始清理资源...")
        if hasattr(server, 'force_exit'):
            server.force_exit = True
        await vertex_client.close()
        logger.success("资源清理完成，服务已安全关闭")


def main_sync() -> None:
    from src.core.config import load_config
    config = load_config()
    configure_logging(debug=config.get("debug", False), log_dir=config.get("log_dir", "logs"))
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main_sync()
