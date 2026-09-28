"""内核错误类型。"""


class CoreError(Exception):
    """内核基类错误。"""


class StoreCorruption(CoreError):
    """节点存储自相矛盾（摘要寻址不到、引用断裂等）：无法判定。"""


class InvalidUpdate(CoreError):
    """更新条目非法（键宽错误等）。"""
