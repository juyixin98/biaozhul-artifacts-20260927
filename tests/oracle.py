"""独立对照预言机：用与被测内核完全不同的写法重新实现三方合并规则。

约束：
* 不 import table_merge.merge_kernel / service / storage（只共享测试常量）；
* 规则来源是题目行为约定，而不是被测代码的输出；
* 返回结构化期望：每个主键的 (decision, 自动结果行或 None)。

集成测试与属性（随机）测试都用它做交叉验证——参考答案不允许全部由被测核心
实现自身生成。
"""
from __future__ import annotations

# 与领域枚举的字符串值保持一致（这里故意不导入枚举，避免耦合实现）
UNCHANGED = "UNCHANGED"
FAST_FORWARD = "FAST_FORWARD"
FIELD_MERGE = "FIELD_MERGE"
SAME_FIELD_CONFLICT = "SAME_FIELD_CONFLICT"
DELETE_MODIFY_CONFLICT = "DELETE_MODIFY_CONFLICT"
ADD_ADD_CONFLICT = "ADD_ADD_CONFLICT"


def _pk(row: dict, pks: tuple[str, ...]):
    return tuple(row[k] for k in pks)


def _index(rows: list[dict], pks: tuple[str, ...]) -> dict[tuple, dict]:
    out: dict[tuple, dict] = {}
    for r in rows:
        out[_pk(r, pks)] = r
    return out


def oracle_merge(
    columns: list[str],
    pks: tuple[str, ...],
    base: list[dict],
    dev: list[dict],
    main: list[dict],
) -> dict[tuple, tuple[str, dict | None]]:
    """返回 key -> (decision 字符串, 自动合并后的行；冲突时为 None)。

    行的删除用“结果中不存在该 key”表达。
    """
    b, d, m = _index(base, pks), _index(dev, pks), _index(main, pks)
    result: dict[tuple, tuple[str, dict | None]] = {}

    for k in sorted(set(b) | set(d) | set(m)):
        rb, rd, rm = b.get(k), d.get(k), m.get(k)

        if rb is None:
            # 共同祖先中不存在：纯新增情形
            if rd is not None and rm is None:
                result[k] = (FAST_FORWARD, dict(rd))
            elif rm is not None and rd is None:
                result[k] = (FAST_FORWARD, dict(rm))
            elif rd == rm:
                result[k] = (FIELD_MERGE, dict(rd))       # 相同新增，收敛
            else:
                result[k] = (ADD_ADD_CONFLICT, None)
            continue

        # 祖先中存在
        dev_deleted = rd is None
        main_deleted = rm is None

        if dev_deleted and main_deleted:
            result[k] = (FAST_FORWARD, None)              # 双方都删
            continue

        if dev_deleted or main_deleted:
            survivor = rm if dev_deleted else rd
            changed = [c for c in columns if rb.get(c) != survivor.get(c)]
            if changed:
                result[k] = (DELETE_MODIFY_CONFLICT, None)
            else:
                result[k] = (FAST_FORWARD, None)          # 一侧删、另一侧原样保留
            continue

        dev_changed = [c for c in columns if rb.get(c) != rd.get(c)]
        main_changed = [c for c in columns if rb.get(c) != rm.get(c)]
        if not dev_changed and not main_changed:
            result[k] = (UNCHANGED, dict(rb))
        elif dev_changed and not main_changed:
            result[k] = (FAST_FORWARD, dict(rd))
        elif main_changed and not dev_changed:
            result[k] = (FAST_FORWARD, dict(rm))
        else:
            clash = [c for c in dev_changed
                     if c in main_changed and rd.get(c) != rm.get(c)]
            if clash:
                result[k] = (SAME_FIELD_CONFLICT, None)
            else:
                merged = {}
                for c in columns:
                    if c in dev_changed:
                        merged[c] = rd[c]
                    elif c in main_changed:
                        merged[c] = rm[c]
                    else:
                        merged[c] = rb[c]
                result[k] = (FIELD_MERGE, merged)
    return result
