"""算法索引层（核心）。

使用成熟分段库 grapheme 0.6.0（Unicode 13 GCB 表）做扩展字素簇分段，
双向索引、边界判定与编辑更新均自行实现，不按字符数猜边界。

三套互相可换算的位置：
- byte   ：UTF-8 字节偏移，取值区间 [0, byte_count]，合法值必须是码点边界
- codepoint：码点偏移（Python str 下标），[0, cp_count]，编辑时必须是簇边界
- cluster：扩展字素簇序号，[0, cluster_count]

索引数组（不可变快照的一部分）：
- cp_to_byte[k]：第 k 个码点起始字节偏移，末尾追加 byte_count
- cp_to_cluster[k]：第 k 个码点所属簇序号
- cluster_to_cp[c]：第 c 个簇起始码点偏移，末尾追加 cp_count
- cluster_to_byte[c]：第 c 个簇起始字节偏移，末尾追加 byte_count（派生）
"""
from __future__ import annotations

from dataclasses import dataclass

import grapheme
from grapheme.grapheme_property_group import get_group
from grapheme.grapheme_property_group import GraphemePropertyGroup as G

from .errors import (
    NotABoundaryError,
    PositionOutOfRangeError,
    UnsupportedSpaceError,
)

# 硬性断行：其左右一定是簇边界（CR/LF 的成对规则在窗口函数中单独处理）。
_HARD_GROUPS = frozenset({G.CR, G.LF, G.CONTROL})

Space = str  # "byte" | "codepoint" | "cluster"
SPACES = frozenset({"byte", "codepoint", "cluster"})


@dataclass(frozen=True)
class TextIndex:
    """给定文本的完整双向索引。"""

    text: str
    cp_to_byte: tuple[int, ...]        # 长度 = cp_count + 1
    cp_to_cluster: tuple[int, ...]     # 长度 = cp_count
    cluster_to_cp: tuple[int, ...]     # 长度 = cluster_count + 1
    cluster_to_byte: tuple[int, ...]   # 长度 = cluster_count + 1（派生）

    # ── 基本量 ──────────────────────────────────────────────────────────────
    @property
    def byte_count(self) -> int:
        return self.cp_to_byte[-1]

    @property
    def cp_count(self) -> int:
        return len(self.cp_to_cluster)

    @property
    def cluster_count(self) -> int:
        return len(self.cluster_to_cp) - 1

    # ── 正向转换 ────────────────────────────────────────────────────────────
    def to_bytes(self, pos: int, space: Space) -> int:
        if space == "byte":
            return self._check_byte(pos)
        if space == "codepoint":
            self._check_cp(pos)
            return self.cp_to_byte[pos]
        if space == "cluster":
            self._check_cluster(pos)
            return self.cluster_to_byte[pos]
        raise UnsupportedSpaceError(f"不支持的位置空间: {space!r}", details={"space": space})

    def to_codepoints(self, pos: int, space: Space) -> int:
        if space == "byte":
            return self._byte_to_cp(pos)
        if space == "codepoint":
            self._check_cp(pos)
            return pos
        if space == "cluster":
            self._check_cluster(pos)
            return self.cluster_to_cp[pos]
        raise UnsupportedSpaceError(f"不支持的位置空间: {space!r}", details={"space": space})

    def to_clusters(self, pos: int, space: Space) -> int:
        if space == "cluster":
            self._check_cluster(pos)
            return pos
        cp = self.to_codepoints(pos, space)
        if cp == self.cp_count:
            return self.cluster_count
        return self.cp_to_cluster[cp]

    def convert(self, pos: int, source: Space, target: Space) -> int:
        if target == "byte":
            return self.to_bytes(pos, source)
        if target == "codepoint":
            return self.to_codepoints(pos, source)
        if target == "cluster":
            return self.to_clusters(pos, source)
        raise UnsupportedSpaceError(f"不支持的位置空间: {target!r}", details={"space": target})

    # ── 边界合法性（编辑位置必须同时是字节边界与簇边界）──────────────────────
    def validate_edit_position(self, pos: int, space: Space) -> int:
        """返回该位置对应的码点偏移；非簇边界/非字节边界时抛 NOT_A_BOUNDARY。"""
        if space == "byte":
            cp = self._byte_to_cp(pos)
        elif space == "codepoint":
            self._check_cp(pos)
            cp = pos
        elif space == "cluster":
            self._check_cluster(pos)
            return self.cluster_to_cp[pos]
        else:
            raise UnsupportedSpaceError(f"不支持的位置空间: {space!r}", details={"space": space})

        if cp == self.cp_count or self._is_cluster_start(cp):
            return cp
        cid = self.cp_to_cluster[cp]
        start, end = self.cluster_to_cp[cid], self.cluster_to_cp[cid + 1]
        raise NotABoundaryError(
            "编辑位置落在扩展字素簇内部",
            details={
                "input_space": space,
                "input_position": pos,
                "codepoint_position": cp,
                "nearest_boundaries": {
                    "cluster": cid,
                    "cluster_start_codepoint": start,
                    "cluster_end_codepoint": end,
                    "cluster_start_byte": self.cp_to_byte[start],
                    "cluster_end_byte": self.cp_to_byte[end],
                },
            },
        )

    def _is_cluster_start(self, cp_pos: int) -> bool:
        if cp_pos == 0 or cp_pos == self.cp_count:
            return True
        return self.cp_to_cluster[cp_pos] != self.cp_to_cluster[cp_pos - 1]

    # ── 内部范围检查 ────────────────────────────────────────────────────────
    def _check_byte(self, pos: int) -> int:
        if not 0 <= pos <= self.byte_count:
            raise PositionOutOfRangeError(
                "字节位置越界",
                details={"position": pos, "max": self.byte_count, "space": "byte"},
            )
        return pos

    def _check_cp(self, pos: int) -> int:
        if not 0 <= pos <= self.cp_count:
            raise PositionOutOfRangeError(
                "码点位置越界",
                details={"position": pos, "max": self.cp_count, "space": "codepoint"},
            )
        return pos

    def _check_cluster(self, pos: int) -> int:
        if not 0 <= pos <= self.cluster_count:
            raise PositionOutOfRangeError(
                "字素簇位置越界",
                details={"position": pos, "max": self.cluster_count, "space": "cluster"},
            )
        return pos

    def _byte_to_cp(self, pos: int) -> int:
        self._check_byte(pos)
        lo, hi = 0, len(self.cp_to_byte) - 1  # cp_to_byte 单调递增，二分
        while lo < hi:
            mid = (lo + hi) >> 1
            if self.cp_to_byte[mid] < pos:
                lo = mid + 1
            else:
                hi = mid
        if self.cp_to_byte[lo] != pos:
            prev_cp = lo - 1
            raise NotABoundaryError(
                "字节位置落在多字节 UTF-8 序列内部",
                details={
                    "input_position": pos,
                    "codepoint_before": prev_cp,
                    "codepoint_after": lo,
                    "nearest_boundaries": {
                        "start_byte": self.cp_to_byte[prev_cp] if prev_cp >= 0 else None,
                        "end_byte": self.cp_to_byte[lo],
                    },
                },
            )
        return lo

    # ── 簇切片（诊断/查询用）────────────────────────────────────────────────
    def cluster_text(self, cluster: int) -> str:
        self._check_cluster(cluster)
        if cluster == self.cluster_count:
            return ""
        s, e = self.cluster_to_cp[cluster], self.cluster_to_cp[cluster + 1]
        return self.text[s:e]


# ── 构建 ────────────────────────────────────────────────────────────────────
def build_index(text: str) -> TextIndex:
    """对整段文本构建双向索引（完整重建路径，也是增量更新的对照基准）。"""
    cp_to_byte: list[int] = [0]
    for ch in text:
        cp_to_byte.append(cp_to_byte[-1] + len(ch.encode("utf-8")))

    cluster_starts: list[int] = []
    cp_to_cluster: list[int] = []
    offset = 0
    for cluster_id, cluster in enumerate(grapheme.graphemes(text)):
        cluster_starts.append(offset)
        clen = len(cluster)
        cp_to_cluster.extend([cluster_id] * clen)
        offset += clen
    cluster_starts.append(offset)

    assert offset == len(text), "分段库返回的码点数与原文不一致"
    cluster_to_byte = tuple(cp_to_byte[c] for c in cluster_starts)

    return TextIndex(
        text=text,
        cp_to_byte=tuple(cp_to_byte),
        cp_to_cluster=tuple(cp_to_cluster),
        cluster_to_cp=tuple(cluster_starts),
        cluster_to_byte=cluster_to_byte,
    )


# ── 增量编辑 ────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Edit:
    """一次区间替换：以簇内码点位置给出 [start_cp, end_cp)，插入 replacement。"""

    start_cp: int
    end_cp: int
    replacement: str


def plan_edit(index: TextIndex, start: int, end: int, space: Space, replacement: str) -> Edit:
    """把外部编辑请求（任意位置空间）换算为码点空间的 Edit，并强制边界合法。"""
    start_cp = index.validate_edit_position(start, space)
    end_cp = index.validate_edit_position(end, space)
    if start_cp > end_cp:
        raise PositionOutOfRangeError(
            "编辑区间起点大于终点",
            details={"start": start_cp, "end": end_cp, "space": "codepoint"},
        )
    return Edit(start_cp=start_cp, end_cp=end_cp, replacement=replacement)


def apply_edit(index: TextIndex, edit: Edit) -> TextIndex:
    """增量应用编辑：字节/码点数组直接拼接，簇数组只重算“受影响窗口”。

    窗口（见 _resync_window）两侧停在与上下文无关的硬断行边界
    （CR/LF/Control，并特殊保证 CRLF 成对、RI 成对），窗口内部用与
    build_index 相同的分段库重切。组合符、ZWJ 表情序列、旗帜 RI 奇偶、
    CRLF 等跨接缝合并都由状态机在窗口内正确处理，窗口外簇保持不变。
    """
    s, e, repl = edit.start_cp, edit.end_cp, edit.replacement
    old = index.text
    new_text = old[:s] + repl + old[e:]
    cp_delta = len(repl) - (e - s)

    # ── 字节索引：前缀偏移不变；替换段从 s 的字节基准重计；后缀整体平移 ──
    base_byte = index.cp_to_byte[s]
    repl_bytes = [0]
    for ch in repl:
        repl_bytes.append(repl_bytes[-1] + len(ch.encode("utf-8")))
    old_region_bytes = index.cp_to_byte[e] - index.cp_to_byte[s]
    suffix_b = tuple(
        b - old_region_bytes + repl_bytes[-1] for b in index.cp_to_byte[e:]
    )
    new_cp_to_byte = (
        index.cp_to_byte[:s]
        + tuple(base_byte + rb for rb in repl_bytes)  # 含替换段起点（= 后缀基准则）
        + suffix_b[1:]
    )
    assert new_cp_to_byte[-1] == len(new_text.encode("utf-8"))
    assert len(new_cp_to_byte) == len(new_text) + 1

    # ── 簇重分段窗口（新文本坐标）──────────────────────────────────────
    win_l, win_r = _resync_window(old, s, e, new_text, cp_delta)

    left_cluster = (
        index.cp_to_cluster[win_l] if win_l < index.cp_count else index.cluster_count
    )
    sub = build_index(new_text[win_l:win_r])

    # 拼接规则（三段端点不重复）：
    #   pre  = 旧簇起点直到 win_l（含 win_l 这个簇起点）
    #   mid  = 窗口内簇起点，相对 win_l 平移；排除窗口自身的 0 起点（与 pre 末点重合），
    #          保留窗口末端 win_r（它同时是后缀首个簇的起点）
    #   post = 严格位于旧 r 之后的旧簇起点，平移 cp_delta
    pre_starts = index.cluster_to_cp[: left_cluster + 1]
    assert pre_starts[-1] == win_l
    mid_starts = tuple(win_l + c for c in sub.cluster_to_cp[1:])
    old_win_r = win_r - cp_delta
    if old_win_r < index.cp_count:
        right_cluster = index.cp_to_cluster[old_win_r]
        post_starts = tuple(
            c + cp_delta for c in index.cluster_to_cp[right_cluster + 1:]
        )
    else:
        post_starts = ()
    new_cluster_to_cp = pre_starts + mid_starts + post_starts

    new_cp_to_cluster = _fill_cp_to_cluster(new_cluster_to_cp, len(new_text))
    new_cluster_to_byte = tuple(new_cp_to_byte[c] for c in new_cluster_to_cp)

    result = TextIndex(
        text=new_text,
        cp_to_byte=new_cp_to_byte,
        cp_to_cluster=new_cp_to_cluster,
        cluster_to_cp=new_cluster_to_cp,
        cluster_to_byte=new_cluster_to_byte,
    )
    assert result.cluster_to_byte == tuple(
        result.cp_to_byte[c] for c in result.cluster_to_cp
    )
    return result


def _fill_cp_to_cluster(cluster_to_cp: tuple[int, ...], cp_count: int) -> tuple[int, ...]:
    out = [0] * cp_count
    for c in range(len(cluster_to_cp) - 1):
        a, b = cluster_to_cp[c], cluster_to_cp[c + 1]
        for k in range(a, b):
            out[k] = c
    return tuple(out)


def _resync_window(
    old: str, s: int, e: int, new_text: str, cp_delta: int
) -> tuple[int, int]:
    """求增量重分段窗口 [win_l, win_r)（返回的是**新文本**码点坐标）。

    为什么需要窗口：GCB 状态机有跨码点状态——CR×LF、Extend/ZWJ 附着链、
    Prepend、RI 两两配对——编辑点两侧的码点可能因此重新并簇。窗口选取：

    1. 在旧文本上从编辑区间两端向外侧行走，直到遇到硬断行符
       （CR/LF/Control），它保证两侧簇划分与对方无关；
    2. 映射到新文本后，再修正跨边界特例：CRLF 必须成对留在窗口内；
    3. 窗口外侧紧邻的 RI 连续段以偶数奇偶对齐，避免旗帜配对外移。
    """
    n = len(old)
    l, r = s, e
    while l > 0 and get_group(old[l - 1]) not in _HARD_GROUPS:
        l -= 1
    while r < n and get_group(old[r]) not in _HARD_GROUPS:
        r += 1

    # ── 旧坐标 CRLF 修正 ──
    # 左端外侧紧接 CR：其配对 LF 可能在编辑区内被删/被插内容隔开，纳入 CR 重切
    if l > 0 and old[l - 1] == "\r":
        l -= 1
    # 右端停在 LF 且其前是 CR：LF 单独不是旧簇起点，整对 CRLF 纳入窗口
    if r < n and old[r] == "\n" and r > 0 and old[r - 1] == "\r":
        r += 1

    win_l, win_r = l, r + cp_delta  # 映射到新文本坐标

    # ── CRLF 成对修正：接缝两侧的 CR/LF 必须同处窗口内 ──
    if (
        win_l > 0
        and win_l < win_r
        and new_text[win_l - 1] == "\r"
        and new_text[win_l] == "\n"
    ):
        win_l -= 1
    if (
        0 < win_r <= len(new_text) - 1
        and new_text[win_r - 1] == "\r"
        and new_text[win_r] == "\n"
    ):
        win_r += 1

    assert 0 <= win_l <= win_r <= len(new_text)
    return win_l, win_r
