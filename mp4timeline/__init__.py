"""受限非加密 MP4 时间线解析服务。

模块划分见 README.md：
- ``mp4parse``  : 媒体盒 / 样本表 / edit list 解析（结构层）
- ``timeline``  : DTS/PTS、timescale 有理数转换、edit list 呈现映射（时间内核）
- ``jobs``      : SQLite 作业状态
- ``api``       : FastAPI 校验 / 作业接口
- ``validation``: 内置证据校验套件
"""

__version__ = "1.0.0"
