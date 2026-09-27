"""持久 posting 列表布尔查询服务。

模块划分（各自承担实际工作）：
- app.config            配置
- app.postings          算法索引：分块 posting 列表 + AND/OR/NOT 合并算子
- app.storage           版本存储：SQLite 中显式版本化的文档全集与索引
- app.query             查询：文本规范（词法/语法）、AST、执行引擎、执行统计
- app.diagnostics       诊断：请求身份、结构化日志、轨迹留存
- app.api               FastAPI HTTP 服务
"""
