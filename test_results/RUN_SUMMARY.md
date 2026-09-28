# 测试运行记录

## 最终状态

- 命令：`.venv/bin/python -m pytest tests/ -v --junitxml=test_results/pytest.xml`
- 结果：**82 passed, 0 failed, 0 skipped**（`pytest.xml` 机器可读，
  `pytest.out` 完整输出）。
- 静态检查：`python -m compileall` 通过；`pyflakes src tests examples` 无告警。
- 端到端：真实启动 uvicorn（127.0.0.1:8011）后运行 `examples/requests.sh`
  与 `examples/arrow_example.py`，输出分别保存在 `examples.out`、
  `arrow_example.out`；服务日志在 `server.log`。
- 版本：见 `logs/session-info.log`（Python 3.12.3 / pyarrow 25.0.1 /
  fastapi 0.141.1 / pytest 9.1.1 / sqlite 3.45.1）。

## 开发期间实际出现、随后修复的失败（如实保留，非当前未决项）

1. **`tests/conftest.py` 相对导入失败**（无包上下文）→ 增加 `tests/__init__.py`。
2. **字典内 NULL 被内核判成 `UNSUPPORTED_VALUE_TYPE` 而非
   `DICTIONARY_CONTAINS_NULL`** → 内核 `_value_type` 显式识别 None，内核自身
   强制 NULL 不得作为值（适配器无法绕过），测试转绿。
3. **元数据 `get_run` 查询位于 SQLite 连接上下文之外**（IndentationError /
   关闭连接后取数）→ 全部查询收回 `with conn` 块。
4. **重复 run_id 的失败记录用 `INSERT OR REPLACE` 覆盖了已成功的 run**
   （成功 run 被改成 status=failed）→ 改 `INSERT OR IGNORE`，仅在真正写入
   failed 行时清理其附属行；`test_duplicate_runid_does_not_corrupt_first_run`
   专门防回归。
5. **HTTP 测试用了 Flask 风格 `.get_json()`** → 统一改为 Starlette `.json()`。
6. **Arrow 输入把 `RecordBatch.column()` 当 ChunkedArray 调 `.chunks`**
   （AttributeError）→ 按普通数组收集，跨 record batch 时才 chunked 合并。
7. **Arrow 输出/夹具按列并排放置不等长批次**（ArrowInvalid: 矩形表长度不一致）
   → 输出改为纵向长表（batch_id/global_code/valid），夹具用 NULL 行补齐，
   顺带再次覆盖位图独立性。
8. **测试误用 `pa.compute.equal`（未导入子模块）** → `import pyarrow.compute as pc`。
9. **示例脚本 `BASE` 写死 8000 端口** → 改为读环境变量。

## 未执行项

无。需求列出的全部场景（重复字典项、空字典、全部 NULL、256/257 位宽阈值的
拒绝与扩容、顺序不变性及其范围边界、元数据原子事务、Arrow IPC、独立验证接口、
分类错误、日志可关联性、配置层）均有对应自动化测试并通过。
