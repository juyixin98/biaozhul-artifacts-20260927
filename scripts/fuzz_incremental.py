"""增量更新 vs 完整重建 的属性测试脚本（开发期快速校验，正式测试见 tests/）。

编辑位置一律取自簇边界（服务契约：编辑不能落在非法边界），
在 cluster / byte / codepoint 三种位置空间分别发起。
"""
import random

from app.indexing import apply_edit, build_index, plan_edit

# 合成字符池：ASCII、空格/Tab、组合符、ZWJ、Emoji 与肤色、RI、CR/LF、控制符、Hangul
POOL = list(
    "ab eé\t"
    + "́̈⃣"          # combining acute / diaeresis / enclosing keycap
    + "‍"                # ZWJ
    + "👨👩👧👦🏽❤️#*"
    + "🇦🇧🇨🇽"
    + "\r\n\0"
    + "ᄀᄂᆨᆫ가"
)


def rand_text(rng: random.Random) -> str:
    return "".join(rng.choice(POOL) for _ in range(rng.randrange(0, 30)))


def same_index(a, b) -> bool:
    return (
        a.cp_to_byte == b.cp_to_byte
        and a.cp_to_cluster == b.cp_to_cluster
        and a.cluster_to_cp == b.cluster_to_cp
        and a.text == b.text
    )


def run_round(rng: random.Random, cases: int, space: str, mismatches: list) -> None:
    for run in range(cases):
        text = rand_text(rng)
        idx = build_index(text)
        starts = list(idx.cluster_to_cp)  # 所有编辑位置必须是簇边界
        cp1 = rng.choice(starts)
        cp2 = rng.choice([c for c in starts if c >= cp1])
        repl = rand_text(rng)
        if space == "cluster":
            cp_to_cluster_id = {st: cid for cid, st in enumerate(idx.cluster_to_cp)}
            p1, p2 = cp_to_cluster_id[cp1], cp_to_cluster_id[cp2]
        elif space == "byte":
            p1, p2 = idx.cp_to_byte[cp1], idx.cp_to_byte[cp2]
        else:
            p1, p2 = cp1, cp2
        edit = plan_edit(idx, p1, p2, space, repl)
        inc = apply_edit(idx, edit)
        full = build_index(inc.text)
        if not same_index(inc, full):
            mismatches.append((space, run, text, p1, p2, repl))
            if len(mismatches) <= 8:
                print(
                    f"MISMATCH space={space} run={run} text={text!r} "
                    f"p1={p1} p2={p2} repl={repl!r}"
                )
                print(" inc cl2cp:", inc.cluster_to_cp)
                print(" full cl2cp:", full.cluster_to_cp)
                print(" inc byte:", inc.cp_to_byte)
                print(" full byte:", full.cp_to_byte)


def main() -> int:
    rng = random.Random(20260928)
    cases = 20000
    mismatches: list = []
    for space in ("cluster", "codepoint", "byte"):
        run_round(rng, cases, space, mismatches)
    print(f"ran {3*cases} edits, mismatches={len(mismatches)}")
    return 1 if mismatches else 0


if __name__ == "__main__":
    raise SystemExit(main())
