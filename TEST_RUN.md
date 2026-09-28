# 测试运行报告

- 环境：Python 3.12.3 / Ubuntu（Linux 6.8），独立 venv
- 依赖：见 `requirements.lock`（fastapi 0.115.6 / pydantic 2.10.4 / pytest 8.3.4）
- 命令：`python -m pytest`
- **结果：288 passed，0 failed，0 skipped，0 xfail（6.5 s）**
  （1 条第三方 DeprecationWarning：starlette TestClient 对 anyio 别名的引用，
  来自依赖内部，不影响功能。）

## 覆盖内容

| 文件 | 数量重点 |
|---|---|
| `test_editdistance.py` | 手工已知距离；OSA 限制性递推负对照；短串全枚举（3 字母表、长 0–4，约 1.1 万对）验证 DL ≤ OSA 且真实分叉 ≥5 处，分叉对逐一过独立 Dijkstra oracle；重复字符、交换链、非对称 insert/delete、有向替换表、零/负代价；60 组固定种子随机对拍 |
| `test_index_bounds.py` | 120 组单位代价 + 60 组加权随机串断言两个下界可接纳（≤ 真实距离）；多阈值（0/0.5/1.0/1.5/2.0/3.0）断言阈值内候选无一被剪枝；长度桶与稳定序 |
| `test_normalization.py` | NFKC 全角、casefold、空白折叠、空结果的具体输出与步骤断言 |
| `test_query.py` | 阈值 `<=` 边缘保留、边缘 uncertain 单列、`(distance, word)` 稳定排序、空串/超长失败码、候选展开上限 notes、路径类型、7 阶段诊断顺序 |
| `test_api.py` | 端到端具体候选、`x-request-id` 透传、空串 422 `empty_query`、超长 422 `query_too_long`、负阈值 422、负代价 422、版本创建/激活/404/409 冲突 |

## 参考答案独立性

`tests/oracle.py` 不调用被测 DP：它以**真实字符串为状态**、四种具体编辑操作为
边，用 `heapq` Dijkstra 求全局最短路，并在返回路径上重放 + 重算成本。
被测实现既不生成 oracle 答案，也不参与其校验。

## 手工冒烟（实际执行）

`uvicorn` 起服务后实际请求验证：

- `speling` → `spelling`，距离 1.0，路径 `insert index=3 char=l`
- `recieve`（swap=0.6）→ `receive`，距离 0.6，路径 `swap index=3 ie<->ei`
- `QUICK` → `quick`，规范化步骤含 `casefold`
- `hlelo` → `hello`，距离 1.0，路径 `swap index=1 le<->el`
- 纯空白查询 → HTTP 422 `failure_code=empty_query`，响应与结构化日志均带 request_id

## 失败/未执行项

- 无失败、无跳过用例。
- 未覆盖（明确不在范围）：按具体字符变化的 insert/delete 单价（会破坏
  last-occurrence 递推的支配关系，README“关键取舍”已说明）；多进程部署下的
  缓存同步；键盘/语言模型集成；真实业务词典。
