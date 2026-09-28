"""两级裁剪后端：

* transforms  固定时区/日期变换（dateconv）
* model       谓词、统计、决策数据模型
* kernel      分区 + 文件统计保守裁剪内核
* adapter     PyArrow/Parquet 格式适配与统计提取
* catalog     SQLite 元数据事务层
* validation  PyArrow 全扫描独立参考真值
* api/service HTTP 验证接口与编排
"""
from .versions import (DATE_TRANSFORM_VERSION, KERNEL_VERSION,
                       STATS_FORMAT_VERSION, version_bundle)

__all__ = ["DATE_TRANSFORM_VERSION", "KERNEL_VERSION",
           "STATS_FORMAT_VERSION", "version_bundle"]
