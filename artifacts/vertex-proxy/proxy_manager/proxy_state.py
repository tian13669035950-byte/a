"""全局代理状态管理（含多节点自动轮换）"""

from typing import Any

_current_proxy: str | None = None
_nodes: list[dict[str, Any]] = []
_current_index: int = -1
_rotation_count: int = 0   # 记录本轮已轮换次数，转满一圈触发重拉订阅


def set_proxy(proxy: str | None):
    global _current_proxy
    _current_proxy = proxy


def get_proxy() -> str | None:
    return _current_proxy


def set_nodes(nodes: list[dict[str, Any]], current_index: int = -1):
    """保存订阅节点列表和当前选中的节点下标，同时重置轮换计数"""
    global _nodes, _current_index, _rotation_count
    _nodes = nodes
    _current_index = current_index
    _rotation_count = 0


def get_node_count() -> int:
    return len(_nodes)


def needs_refresh() -> bool:
    """是否已轮换满一圈（所有节点都试过了）"""
    return len(_nodes) > 0 and _rotation_count >= len(_nodes)


def reset_rotation_count():
    """重拉订阅后调用，重置计数"""
    global _rotation_count
    _rotation_count = 0


def rotate_to_next() -> bool:
    """
    切换到下一个订阅节点并重启 xray。
    成功返回 True，节点为空或切换失败返回 False。
    转满一圈后 needs_refresh() 将返回 True，外部可据此触发重拉订阅。
    """
    global _current_index, _current_proxy, _rotation_count

    if not _nodes:
        return False

    next_index = (_current_index + 1) % len(_nodes)

    from .xray_manager import start_xray
    node = _nodes[next_index]
    ok, _ = start_xray(node)
    if ok:
        _current_index = next_index
        _current_proxy = "socks5://127.0.0.1:1080"
        _rotation_count += 1
        return True
    return False
