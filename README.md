# 湖表读时删除应用器

文件行号删除 + 主键等值删除的纯后端服务（FastAPI / PyArrow / SQLite）。

**完整文档见 [`docs/README.md`](docs/README.md)**（语义边界、NULL 规则、
错误分类、未执行检查清单）。

## 一分钟运行

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt && pip install -e .

pytest -q                          # 67 个单元/场景/属性测试
python scripts/verify_service.py   # 28 项真实 HTTP 端到端检查
deleter demo                       # 打印每一行被保留/删除的依据
```

## 语义要点

- **位置删除绑定文件内容身份**：重写产生新版本后，旧行号删除一律失效、
  不复用（细分 stale_row_already_removed / stale_file_rewritten / out_of_range）。
- **等值删除按序列号可见范围应用**：只删除删除发生时已存在的行，
  先删后插的同键新行必须保留。
- **NULL 遵循 SQL 三值逻辑**：`k = NULL` 不删任何行，NULL 行不匹配任何谓词。
- **过滤/列裁剪在删除之后执行**，不改变删除语义。

参考答案三重独立：人工黄金夹具、不依赖被测代码的参考预言机、被测内核；
随机操作流逐行对照。每次操作都有 `run_id` 与可重放的内核判断 trace。
