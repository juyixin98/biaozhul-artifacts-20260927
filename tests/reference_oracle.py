"""独立参考实现（independent oracle）。

验收要求：参考答案不能全部由被测核心实现自身生成。本模块**不 import
merge3.kernel**，而是用最朴素的 Python 重新实现一遍逐键三方分类与
结果行集组装（算法表达刻意不同：直接对三种存在性分支写嵌套判断，
不共享任何工具函数），供测试在大量输入上与被测内核交叉比对。

它只产出"最终行集 + 冲突键及类别"这一可核验契约，字段来源等展示信息
仍由被测内核负责并单独断言。
"""
from __future__ import annotations

from typing import Any


def reference_merge(
    columns: list[str],
    pk: list[str],
    base: list[dict[str, Any]],
    ours: list[dict[str, Any]],
    theirs: list[dict[str, Any]],
) -> dict[str, Any]:
    """返回 {"rows": 自动结果(冲突行不含在内), "conflicts": {key: 类别}, ...}。

    冲突键不出现在 rows 中；非冲突分区按参考规则给出具体行。
    """

    def key(row: dict[str, Any]) -> tuple[Any, ...]:
        return tuple(row[c] for c in pk)

    def kstr(k: tuple[Any, ...]) -> str:
        return "|".join(repr(x) for x in k)

    B = {key(r): r for r in base}
    O = {key(r): r for r in ours}
    T = {key(r): r for r in theirs}

    result: dict[str, dict[str, Any]] = {}
    conflicts: dict[str, str] = {}

    for k in sorted(set(B) | set(O) | set(T)):
        b, o, t = B.get(k), O.get(k), T.get(k)
        name = kstr(k)

        if b is not None and o is not None and t is not None:
            oc = [c for c in columns if b[c] != o[c]]
            tc = [c for c in columns if b[c] != t[c]]
            if not oc and not tc:
                result[name] = dict(b)
            elif oc and not tc:
                result[name] = dict(o)
            elif tc and not oc:
                result[name] = dict(t)
            elif o == t:
                result[name] = dict(o)
            else:
                clash = [c for c in columns if c in oc and c in tc and o[c] != t[c]]
                if clash:
                    conflicts[name] = "field_value_conflict"
                else:
                    merged = dict(b)
                    for c in oc:
                        merged[c] = o[c]
                    for c in tc:
                        merged[c] = t[c]
                    result[name] = merged
        elif b is not None and o is None and t is None:
            pass  # 两边都删
        elif b is not None and (o is None or t is None):
            survivor = t if o is None else o
            if survivor == b:
                pass  # 一方删、另一方没动 -> 删
            else:
                conflicts[name] = "delete_modify_conflict"
        elif b is None and o is not None and t is not None:
            if o == t:
                result[name] = dict(o)
            else:
                conflicts[name] = "add_add_conflict"
        elif b is None and o is not None:
            result[name] = dict(o)
        else:
            result[name] = dict(t)

    return {
        "rows": [result[k] for k in sorted(result)],
        "conflicts": conflicts,
        "result_keys": sorted(result),
    }


def reference_assemble_after_resolution(
    columns: list[str],
    pk: list[str],
    base: list[dict[str, Any]],
    ours: list[dict[str, Any]],
    theirs: list[dict[str, Any]],
    resolutions: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """把显式解决方案（key repr -> {"kind", "row"}）应用后，给出最终行集。

    resolutions 的 row 为最终保留行；kind=delete 表示删除。
    与 reference_merge 共享极少逻辑，仅复用索引写法。
    """

    def key(row: dict[str, Any]) -> tuple[Any, ...]:
        return tuple(row[c] for c in pk)

    def kstr(k: tuple[Any, ...]) -> str:
        return "|".join(repr(x) for x in k)

    auto = reference_merge(columns, pk, base, ours, theirs)
    out = {tuple(r[c] for c in pk): r for r in auto["rows"]}
    for ks, decision in resolutions.items():
        # 从 repr 键找回真实键：在三方键集合里匹配
        real = None
        for k in set((key(r) for r in base)) | set((key(r) for r in ours)) | set(
            (key(r) for r in theirs)
        ):
            if kstr(k) == ks:
                real = k
                break
        if real is None:  # pragma: no cover - 测试夹具错误
            raise AssertionError(f"解决方案引用了未知键 {ks}")
        if decision["kind"] == "delete":
            out.pop(real, None)
        else:
            out[real] = dict(decision["row"])
    return [out[k] for k in sorted(out)]
