"""merge3 — 结构保留的三方文本合并后端。

模块划分:
- app.textnorm   文本规范:行切分、行结束符与末尾换行的显式建模
- app.edits      区间编辑:由共同基线生成半开区间编辑
- app.merge      三路合并算法:冲突规则、冲突块、显式重建
- app.store      版本存储:SQLite 持久化版本与合并记录
- app.diagnostics 诊断:带请求标识的结构化日志与脱敏
- app.api        查询接口:FastAPI 应用
- app.config     配置
"""

__version__ = "0.1.0"
