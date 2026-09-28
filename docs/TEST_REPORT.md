# 测试报告

- 时间（UTC）：2026-09-28 05:25
- 环境：Linux 6.8.0-90-generic · Python 3.12.3 · pytest 9.1.1
- 依赖：`requirements-lock.txt`（pip freeze 全量锁定 32 个包）
- 结果：**63 passed, 0 failed, 1 warning（与本服务无关的 starlette/httpx 弃用提示）**
- 覆盖率（`threshold_service/` 行覆盖）：**96%**（686 语句，27 未覆盖；
  剩余未覆盖主要为 dev 配置回退分支与少量防御性异常分支）

## 如何复现

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-lock.txt
.venv/bin/python -m pytest
.venv/bin/coverage run -m pytest && .venv/bin/coverage report --include="threshold_service/*"
```

另做过真实服务器冒烟（uvicorn + curl，`scripts/smoke.sh`）：
创建 2-of-3 → 2 份恢复 `ACCEPTED` → 1 份恢复 `403 BELOW_THRESHOLD`
→ 跨集合混份 `422 MIXED_SET` → `/audit` 仅返回指纹；
对落盘的 `data_smoke/audit.log` 与 `tss.db` 执行 `grep` 搜索测试秘密明文，
命中次数均为 0。

## 关键验证与需求对应

| 需求 | 对应用例 |
| --- | --- |
| 枚举小配置合法子集恢复相同秘密 | `test_shamir.py::test_every_qualifying_subset_reconstructs_the_same_secret`（2-of-3、3-of-5、4-of-4 × 4 种秘密，枚举全部门限组合） |
| 门限以下不泄露 | `test_below_threshold_does_not_reveal_secret`、内核/API 的 BELOW_THRESHOLD 用例 |
| 混集合 | `test_mixed_set_is_hard_rejected`、`test_mixed_set_http_422` |
| 重复份额不重复计数 | `test_duplicate_identical_share_not_counted_twice`（同指纹交两次仍 403） |
| 同 x 冲突 | `test_duplicate_x_with_conflicting_payload_drops_both`（双方排除） |
| 坏校验和（标签） | `test_bad_tag_detected`、`test_tag_covers_each_bound_identity_field`（逐绑定字段变异）、`test_non_base64_tag_is_malformed_evidence` |
| 参数不兼容 | `test_incompatible_field_params_excluded`、`test_threshold_mismatch_excluded`、`test_unsupported_field_is_refused` |
| 坏长度 | `test_bad_length_excluded` |
| 未知集合 / 畸形证据 | `test_unknown_set_*`、`test_malformed_evidence_category`、非 dict JSON 用例 |
| 恢复失败 ≠ 定位全部恶意参与者 | `test_tampered_authenticated_share_yields_indeterminate_not_wrong_secret`、`test_clean_subset_still_recovers_...`、`test_enum_truncation_flag_when_many_shares`（INDETERMINATE + 文本显式声明 + 预算截断） |
| 参考答案独立性 | `tests/oracle_reference.py`：手写 xtime GF 乘法/穷举逆元/独立 LCG，FIPS-197 已知答案自校验；`test_oracle_and_library_agree_on_split_points` 双向交叉 |
| 日志仅含指纹 | `test_audit_redaction_drops_sensitive_keys`、`test_audit_endpoint_filters_by_request_and_redacts`、冒烟后对磁盘文件 grep=0 |
| 静态加密/状态隔离 | `test_shares_are_encrypted_at_rest`、`test_sets_are_isolated_and_unique_x_enforced`、`test_two_databases_do_not_share_state` |
| 诊断带请求标识与关键状态 | `test_request_id_propagates_to_audit`，每条审计含 `record_id/request_id/stage/outcome/reason/detail` |

断言风格：全部断言**具体值或具体失败类别**（`MIXED_SET`、`BELOW_THRESHOLD`、
`BAD_TAG`、`COMMIT_MISMATCH` 等），不存在仅检查「接口能调用」的用例。

## 过程中出现过的失败（如实保留）

1. 首次运行测试即 `SyntaxError`：`shamir.py` 中 `ShareError` 的 docstring
   少写一个引号（`。""`），修复后通过。
2. 新增边界用例时，`test_non_base64_tag_counts_as_bad_tag` 曾断言为 `BAD_TAG`，
   实际实现把「标签不是合法 base64」在解析阶段归为 `MALFORMED_EVIDENCE`
   （更早、更准确）。判定该行为更合理后**改测试而非改实现**，重命名为
   `test_non_base64_tag_is_malformed_evidence` 并断言 malformed 原因含 `base64`。
   这是仅有的两次红灯，修复后 63 项全绿。

## 未执行 / 不在范围内的验证（明确说明）

- **性能/压力测试未执行**：未对大秘密（>MB 级）、255 份规模或高并发做基准；
  逐字节多项式与子集枚举（预算 256）决定了本实现面向小规模演示。
- **恶意安全（malicious security）未提供，也未测试**：无 Feldman/Pedersen VSS，
  不抵抗分发者/服务端作恶；INDETERMINATE 用例只验证「拒绝给出错误秘密 +
  不做过度归因」，不声称能识别共谋者。
- **主密钥轮换 / KMS 集成未测试**：非 dev 环境必须注入 `TSS_MASTER_KEY` 的
  启动路径仅做了代码检查，未在真实 KMS 环境演练。
- **份额刷新（proactive refresh）、份额删除恢复、密钥吊销**未实现。
- 仅在 Linux/Python 3.12 上运行；Windows/macOS 与其它 Python 版本未执行。
- 依赖做了全量版本锁定，但未做依赖漏洞扫描（如 pip-audit）的 CI 集成。
