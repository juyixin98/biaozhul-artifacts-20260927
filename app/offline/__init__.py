"""离线层：独立验证器与确定性回放。

刻意不导入 app.core / app.api / app.storage 的任何树逻辑：
* ``verifier.py`` 只依赖 app.coding 的哈希原语，重新实现证明展开与核验；
* ``replay.py`` 从 JSONL/SQLite 读流水，用独立树重放，逐版本比对根与签名。

测试会用三套实现（内核、离线、朴素参考 tests/reference）交叉比对，
满足“参考答案不能全部由被测核心实现自身生成”。
"""
