"""全局代理状态管理（含多节点自动轮换）"""

from typing import Any

_current_proxy: str | None = None
_nodes: list[dict[str, Any]] = []
_current_index: int = -1


def set_proxy(proxy: str | None):
    global _current_proxy
    _current_proxy = proxy


def get_proxy() -> str | None:
    return _current_proxy


def set_nodes(nodes: list[dict[str, Any]], current_index: int = -1):
    """保存订阅节点列表和当前选中的节点下标"""
    global _nodes, _current_index
    _nodes = nodes
    _current_index = current_index


def get_node_count() -> int:
    return len(_nodes)


def rotate_to_next() -> bool:
    """
    切换到下一个订阅节点并重启 xray。
    成功返回 True，节点为空或切换失败返回 False。
    """
    global _current_index, _current_proxy

    if not _nodes:
        return False

    next_index = (_current_index + 1) % len(_nodes)
    if next_index == _current_index and len(_nodes) > 1:
        return False

    from .xray_manager import start_xray
    node = _nodes[next_index]
    ok, _ = start_xray(node)
    if ok:
        _current_index = next_index
        _current_proxy = "socks5://127.0.0.1:1080"
        return True
    return False
