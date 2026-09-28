"""格式适配层：Parquet 文件与逻辑类型/内容哈希之间的适配。

文件物理布局（合成湖格式，模拟 Iceberg 数据/删除文件分离）：
- 数据文件：列为表 schema 全部列；date 逻辑类型物理落 date32。
- 位置删除文件：固定两列 (target_file_id: string, position: int64)。
- 等值删除文件：仅包含键列，键值允许为 NULL（扫描时按 SQL NULL 语义不命中任何行）。
所有文件不可变；content_hash = 文件原始字节 SHA-256。
"""
