# 基础费用递推模型（合成 EIP-1559 后端）

一个**完全离线、合成数据**的多模块后端，实现 EIP-1559 式基础费用多区块递推与
交易有效性检查。没有真实链连接、没有生产账号、没有经济预测——所有输入都是本地
合成夹具，密码学使用成熟的 `ecdsa` 库（secp256k1）。

## 技术栈

Python 3.10+ · FastAPI · SQLite（标准库 `sqlite3`）· ecdsa（secp256k1/RFC6979）·
pytest / httpx。版本全部固定于 `requirements.txt`。

## 快速开始

```bash
python3 -m venv .venv && source .venv/bin/activate
make install        # 安装固定依赖
make test           # 84 个 pytest（断言具体数值与失败类别）
make verify         # 47 项端到端验证：手算向量 + 预言机交叉 + 回放/守恒
make replay         # 离线回放合成链，生成 reports/replay_cli.json
make api            # 起 HTTP 服务（127.0.0.1:8000），数据落 data/chain.db
```

或不使用 Make：

```bash
PYTHONPATH=src:. python scripts/verify.py
PYTHONPATH=src:. python -m scripts.replay_blocks --scenario fixtures/chain_fixture.json \
    --report reports/replay_cli.json
PYTHONPATH=src python -m uvicorn basefee.api.app:app
```

## 目录结构

```
config/protocol.json          固定参数（弹性倍数、分母、intrinsic gas、uint256 上限…）
src/basefee/
  params.py                   固定参数加载与校验（冻结）
  errors.py                   稳定错误码枚举
  encoding/                   RLP、SHA-256 标识、secp256k1 签/恢复/验签
  kernel/eip1559.py           纯费用递推（核心机制，无 I/O）
  kernel/execution.py         交易有效性 + 原子区块状态转移 + 费用守恒
  kernel/chain.py             genesis / 父块衔接 / 区块哈希
  storage/                    SQLite 持久索引（大数用十进制 TEXT）
  replay/                     离线回放引擎
  api/                        FastAPI、Pydantic、wire 编解码、结构化日志
reference/oracle.py           独立预言机（禁止 import basefee），另一种实现
fixtures/
  hand_vectors.json           手算向量（附算术草稿），非核心实现生成
  chain_fixture.json          带真实签名的确定性合成链（由预言机生成，已提交）
scripts/
  build_fixtures.py           用预言机重新生成合成链夹具
  replay_blocks.py            离线回放 CLI（含被拒块案例）
  verify.py                   端到端验证闸门（失败退出码非 0）
tests/                        84 个独立测试
docs/SEMANTICS.md             边界语义（除法方向、最小增量、销毁/小费、差异与不做的事）
docs/ARCHITECTURE.md          架构与"独立性"如何保证
docs/ERROR_CODES.md           错误码目录
```

## 核心递推（只依赖父块）

```
target = gas_limit // 2
向上: next = base + max(1, base*(used-target)//target//8)   # 两次 floor；base>0 时至少 +1
向下: next = max(0, base - base*(target-used)//target//8)
持平: next = base
```

交易实际价格 = `base_fee + min(max_tip, max_fee - base_fee)`；base 部分销毁、
tip 部分支付本地 coinbase。精确守恒在测试中被断言（见 `docs/SEMANTICS.md`）。

## HTTP 示例

```bash
curl -s localhost:8000/v1/health
curl -s -X POST localhost:8000/v1/fee/next -H 'content-type: application/json' \
  -H 'X-Request-ID: demo-1' \
  -d '{"parent_base_fee":"1000000000","gas_used":30000000,"gas_limit":30000000}'
# → next_base_fee 1125000000；响应头/体与 JSON 日志都带 request_id 与 protocol_version
```

响应统一形如 `{request_id, component, protocol_version, result, failures[], warnings[]}`，
硬失败与非阻断警告分离。

## 验证结果的诚实性约定

* 期望值来自**手算向量**与**独立预言机**，两者都不通过被测内核生成。
* 未执行/无法执行的检查在验证报告 `not_executed` 中单列，绝不写成已通过。
* 当前 `reports/verification.json` 与 `reports/replay_cli.json` 是最近一次本地运行产物。
