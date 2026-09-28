"""colaudit — 列式文件 min/max、NULL 计数与排序统计的审计后端。

模块划分:
  config   独立配置 (环境变量 / YAML)
  logical  逻辑类型、比较语义 (NaN / 有符号零)
  stats    列统计结构、重算与页->行组聚合
  adapter  格式适配 (Parquet 行组/逻辑页、内嵌统计、声明统计)
  catalog  SQLite 元数据事务
  audit    执行内核: 校验声明统计、聚合关系、生成裁决与诊断
  prune    基于受信统计的剪枝 (坏统计一律拒绝剪枝)
  query    全表扫描 / 剪枝查询执行
  masking  敏感数据脱敏
  api      FastAPI 验证接口
"""

__version__ = "0.1.0"
