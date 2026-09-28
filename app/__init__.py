"""集中式文本 OT 后端（仅插入 / 删除）。

模块边界见 README：
  errors.py   错误分类与数据/错误契约
  models.py   操作（Op）与组件（Component）的文本规范
  ot.py       算法索引：apply / transform（核心）
  storage.py  版本存储：SQLite、历史裁剪、快照
  service.py  服务层：提交/拉取/裁剪/查询的事务编排
  api.py      FastAPI HTTP 适配
  client.py   两个本地模拟客户端（进程内同步或 HTTP）
"""

__all__ = ["errors", "models", "ot", "storage", "service", "client"]
