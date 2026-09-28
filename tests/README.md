# 测试组织

测试不依赖任何外部服务或账号；密钥全部为固定种子合成夹具（见 `conftest.py`）。

| 文件 | 职责 |
|---|---|
| `conftest.py` | 合成密钥、`make_tx` 签名辅助、汇编辅助，以及**独立参考实现**（不导入 VM 语义代码的纯 Python 算术 / gas 函数）和手写期望常量 |
| `test_vm_arithmetic.py` | 整数运算、±2^63 边界、溢出、除零、比较、PUSH 符号扩展 |
| `test_gas_critical.py` | 临界 gas：先扣费后副作用、内存扩张增量、精确耗尽、栈上下溢 |
| `test_vm_nested_calls.py` | CALL 帧隔离、子帧失败传播、gas 退还/全耗、深度上限、轨迹 |
| `test_vm_state_rollback.py` | 写后异常回滚、无效字节码静态拒绝、存储键边界、入参不被修改 |
| `test_determinism.py` | spawn 独立进程重放同一执行 / 同一批签名交易，比对结果与收据摘要；版本与输入绑定 |
| `test_kernel.py` | 验签、链号、查重、批量原子性、失败笔入块与逐笔状态根、dry-run 无副作用 |
| `test_store_replay.py` | SQLite 索引、离线回放 ACCEPT、三类篡改 REJECT、版本不符 UNDETERMINED、跨进程报告摘要 |
| `test_api.py` | FastAPI 状态码、请求标识、422/404、脱敏、冷启动状态重建 |
| `test_encoding_assembler.py` | Ed25519 验签拒绝路径、规范编码、汇编/反汇编、字节码校验 |

运行：

```bash
.venv/bin/python -m pytest                      # 全部
.venv/bin/python -m pytest -k oog -v            # 按关键字
.venv/bin/python -m pytest -p no:randomly --tb=short
```

“参考答案独立性”的实现方式：算术与 gas 的期望值用整数字面量手写，并由
`conftest.py` 中不依赖 `teaching_chain.vm` 的参考函数交叉计算；确定性结论
则由独立 OS 进程而不是同进程二次调用来支撑。
