r"""集中式纯文本 OT 后端（仅插入/删除）。

模块边界见 README：
textmodel -> transform -> engine -> repository -> service -> api
                                         \-> clientsim（模拟客户端复用同一 transform）
"""

__version__ = "1.0.0"
