# 测试运行记录

命令：`source .venv/bin/activate && python -m pytest -v`
平台：Linux x86_64，Python 3.12.3，pytest 9.1.1

## 结果

```
collected 56 items

tests/test_api.py .........                                                          [ 16%]
tests/test_field_math.py ............                                                [ 37%]
tests/test_kernel_recovery.py ...............                                        [ 64%]
tests/test_shamir_core.py ...............                                            [ 91%]
tests/test_state_audit.py .....                                                      [100%]

56 passed, 1 warning in 1.97s
```

唯一告警来自第三方 `starlette.testclient` 对 `httpx` 的弃用提示
（建议未来换 `httpx2`），不影响测试结果。

## 其它验证

- `python scripts/smoke.py` → ALL SMOKE CHECKS PASSED（不依赖 pytest/HTTP）。
- 真实 `uvicorn` 服务 + `scripts/example_requests.sh` 与在线 curl：
  - 合法 3-of-5 子集 → `recovered_unverifiable`，secret 正确；
  - 5/5 份额 → `recovered_verified`；
  - 2 个份额 → `rejected_insufficient_threshold`；
  - 篡改 y → `bad_integrity_mac`；异素数 → `field_parameter_incompatible`；
    混集合 → `wrong_collection`。

## 未执行项 / 前置条件

- 测试需要先安装锁定依赖（`pip install -r requirements.txt`）。在**无网络**
  环境无法安装 galois/numpy/fastapi/pytest 等，测试将无法运行；这是唯一已知
  的“未执行”前置条件。
- 未做：生产部署、身份认证、VSS（Feldman/Pedersen）——属明确的范围外项。

## 开发过程中真实出现并修复的失败（留档，非当前状态）

1. galois `GF(p, compile="python")` 取值错误，应为 `"python-calculate"`；
2. 256-bit 素数建域约 110s（默认重新证明素性并搜索本原元）→ 固定本原元 g=3
   且 `verify=False`，建域降到亚毫秒；
3. `app/core/kernel.py` 相对导入路径写错（`parsing`/`state`/`envelope`）；
4. 多块夹具 49 字节实为 2 块（测试误写成 3）→ 改为 67 字节/3 块；
5. 构造恶意份额时把 JSON 中字符串形式的 `ys` 直接当 int 运算；
6. “失败≠归因”用例的扰动权重落在块内零填充区，内容字节未变 → 改为作用于
   真实内容字节（权重 2^96）。

截至当前提交，全部测试通过，无失败或跳过的用例。
