"""Incremental index updates on edits.

The library segments text; the bidirectional bookkeeping and the
incremental update are ours.  The key guarantee is:

    apply_edit(build_index(t), e).index == build_index(apply_edit_to_text(t, e))

i.e. an incremental edit always produces exactly the same index as a full
rebuild.  Tests cross-check both paths exhaustively.

Strategy — resegment one widened window, never "adjust by character count":

1. The edit is anchored at grapheme cluster boundaries.  Byte anchors must
   also be UTF-8 lead boundaries; codepoint anchors must also be cluster
   boundaries.  Anchors inside a combining sequence / ZWJ sequence / flag
   pair are rejected (the boundary predicates in :mod:`index` enforce this;
   RIS parity, CR×LF etc. all fall out of requiring a true cluster edge).
2. After the replacement text is canonicalized independently, the old and
   new strings are compared by common prefix/suffix, locating the smallest
   changed codepoint region on *both* sides.
3. That region is widened to enclosing old-index cluster boundaries and by
   one cluster on each side, so any context-dependent break — RIS flag
   pairing, CR×LF, ZWJ joining, Extend/SpacingMark chains — is re-decided
   from a clean break.  Only the window is resegmented; the prefix/suffix
   clusters outside it are reused verbatim.
4. Offset tables are spliced and shifted by the byte/codepoint delta.

Normalization note: NFC applied to old text + replacement can change text
outside the literal replaced span (composition at the seams). The
common-prefix/suffix diff between old canonical text and the freshly
normalized full text captures exactly that, so normalization seams are
handled by the same windowing code rather than special cases.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import index as index_mod
from . import normalizer, segmenter
from .errors import EditRangeCrossed, InvalidUnit

UNIT_GRAPHEME = "grapheme"
UNIT_CODEPOINT = "codepoint"
UNIT_BYTE = "byte"
UNITS = (UNIT_GRAPHEME, UNIT_CODEPOINT, UNIT_BYTE)


@dataclass(frozen=True)
class EditResult:
    text: str
    index: index_mod.TextIndex
    #: Codepoint window that was resegmented on the new text (for diagnostics).
    new_window: tuple[int, int]
    #: Clusters before/inside/after the resegmented window (for diagnostics).
    reused_before: int
    rebuilt_clusters: int
    reused_after: int
    delta_codepoints: int
    delta_bytes: int


@dataclass(frozen=True)
class Edit:
    start: int
    end: int
    replacement: str
    unit: str = UNIT_GRAPHEME

    def __post_init__(self) -> None:
        if self.unit not in UNITS:
            raise InvalidUnit(self.unit)
        if self.start > self.end:
            raise EditRangeCrossed(self.start, self.end, self.unit)
        if self.start < 0 or self.end < 0:
            from .errors import PositionOutOfRange
            raise PositionOutOfRange(self.unit, min(self.start, self.end), 0)


def apply_edit(
    old: index_mod.TextIndex,
    edit: Edit,
    *,
    normalization: str = normalizer.DEFAULT_FORM,
    max_clusters: int | None = None,
) -> EditResult:
    """Apply ``edit`` to text+index ``old`` and update the index incrementally.

    Raises the same boundary errors a strict conversion would raise:
    :class:`IllegalByteBoundary`, :class:`IllegalCodepointBoundary`,
    :class:`PositionOutOfRange`.
    """
    start_cp, end_cp = _anchors_to_codepoints(old, edit)

    # Reject anchors that are codepoints but not cluster boundaries (the
    # strict conversions already do this; assert the intent explicitly).
    if not (old.is_codepoint_boundary(start_cp)
            and old.is_codepoint_boundary(end_cp)):
        from .errors import IllegalCodepointBoundary
        bad = start_cp if not old.is_codepoint_boundary(start_cp) else end_cp
        raise IllegalCodepointBoundary(bad)

    old_text = old.text
    # The new text is assembled raw and then canonicalized as a whole,
    # because NFC can compose across the edit seams.
    if normalization.upper() == "NONE":
        replacement = edit.replacement
        from . import encoding
        encoding.ensure_scalar_value(replacement)
        new_text = old_text[:start_cp] + replacement + old_text[end_cp:]
    else:
        raw_new = old_text[:start_cp] + edit.replacement + old_text[end_cp:]
        new_text = normalizer.canonicalize(raw_new, normalization)

    return update_index_incremental(
        old, old_text, new_text, max_clusters=max_clusters
    )


def update_index_incremental(
    old: index_mod.TextIndex,
    old_text: str,
    new_text: str,
    *,
    max_clusters: int | None = None,
) -> EditResult:
    """Core windowed update given the old text/index and the new full text.

    Shared by :func:`apply_edit` and by the pure-normalization path.
    """
    # 1. smallest changed codepoint region on both sides.
    h = _common_prefix(old_text, new_text)
    t_old = len(old_text) - _common_suffix(old_text, new_text, h)
    t_new = len(new_text) - _common_suffix(old_text, new_text, h)
    # h may sit mid-cluster (normalization changed inside a cluster); widen
    # backward to the nearest old cluster boundary.
    win_old_start = _floor_leq(old.cp_start, h)
    win_old_end = _ceil_geq(old.cp_start, t_old)
    k_start = _cluster_index_at(old, win_old_start)
    k_end = _cluster_index_at(old, win_old_end)
    # Widen to a context-safe window: one cluster on each side, then keep
    # extending across context-dependent runs (RIS parity, CR/LF, ZWJ/Extend
    # chains), so nothing outside the window can be re-paired with anything
    # inside it by the segmentation rules.
    k_start, k_end = _widen_safe_window(old, k_start, k_end)
    old_win_s = old.cp_start[k_start]
    old_win_e = old.cp_start[k_end]

    # 2. the same prefix/suffix lengths in bytes & codepoints locate the
    #    matching window on the new string.
    pre_cp = old_win_s
    suf_cp_old = len(old_text) - old_win_e
    new_win_s = pre_cp
    new_win_e = len(new_text) - suf_cp_old
    if new_win_e < new_win_s:
        # Replacement shortened text so much the widened suffix overlaps;
        # fall back to a maximal safe window: whole text.
        new_win_s, new_win_e = 0, len(new_text)
        old_win_s, old_win_e = 0, len(old_text)
        k_start, k_end = 0, old.cluster_count

    # 3. resegment only the new window.
    window = new_text[new_win_s:new_win_e]
    win_spans = segmenter.cluster_spans(window)
    n_new_clusters = old.cluster_count - (k_end - k_start) + len(win_spans)
    if max_clusters is not None and n_new_clusters > max_clusters:
        from .errors import TooManyClusters
        raise TooManyClusters(n_new_clusters, max_clusters)

    # 4. splice: reused prefix, resegmented window, shifted suffix.
    byte_before = old.byte_start[k_start]
    new_cp = [old.cp_start[i] for i in range(k_start)]
    new_by = [old.byte_start[i] for i in range(k_start)]
    cur_cp = new_win_s
    cur_by = byte_before
    prev_end = 0
    for s, e in win_spans:
        new_cp.append(cur_cp)
        new_by.append(cur_by)
        cur_cp = new_win_s + e
        cur_by += len(window[prev_end:e].encode("utf-8"))
        prev_end = e

    # Prefix length is identical on both texts by construction; suffix
    # length is identical too, so the shift is simply (new − old) window end.
    delta_cp = new_win_e - old_win_e
    delta_by = (len(new_text.encode("utf-8")) - (len(new_text) - new_win_e)) \
        - (old.byte_count - (len(old_text) - old_win_e))
    for i in range(k_end, old.cluster_count + 1):
        new_cp.append(old.cp_start[i] + delta_cp)
        new_by.append(old.byte_start[i] + delta_by)

    import array
    idx = index_mod.TextIndex(
        text=new_text,
        cp_start=array.array("q", new_cp),
        byte_start=array.array("q", new_by),
    )
    index_mod._validate_invariants(idx, new_text)

    return EditResult(
        text=new_text,
        index=idx,
        new_window=(new_win_s, new_win_e),
        reused_before=k_start,
        rebuilt_clusters=len(win_spans),
        reused_after=old.cluster_count - k_end,
        delta_codepoints=len(new_text) - len(old_text),
        delta_bytes=len(new_text.encode("utf-8")) - old.byte_count,
    )


# --- helpers ---------------------------------------------------------------


def _anchors_to_codepoints(
    old: index_mod.TextIndex, edit: Edit
) -> tuple[int, int]:
    if edit.unit == UNIT_GRAPHEME:
        s = old.cluster_to_codepoint(edit.start)
        e = old.cluster_to_codepoint(edit.end)
    elif edit.unit == UNIT_CODEPOINT:
        # strict=True rejects out-of-range offsets and codepoints that are not
        # cluster boundaries (e.g. a combining mark mid-sequence).
        old.codepoint_to_cluster(edit.start, strict=True)
        old.codepoint_to_cluster(edit.end, strict=True)
        s, e = edit.start, edit.end
    else:  # byte
        # strict=True rejects continuation bytes and non-cluster leads.
        s = old.byte_to_codepoint(edit.start, strict=True)
        e = old.byte_to_codepoint(edit.end, strict=True)
    return s, e


def _common_prefix(a: str, b: str) -> int:
    n = min(len(a), len(b))
    i = 0
    while i < n and a[i] == b[i]:
        i += 1
    return i


def _common_suffix(a: str, b: str, skip_first: int = 0) -> int:
    """Length of longest common suffix, not crossing ``skip_first`` chars."""
    ia = len(a) - 1
    ib = len(b) - 1
    n = 0
    while ia >= skip_first and ib >= skip_first and a[ia] == b[ib]:
        ia -= 1
        ib -= 1
        n += 1
    return n


def _floor_leq(starts, value: int) -> int:
    lo, hi = 0, len(starts)
    while lo < hi:
        mid = (lo + hi) // 2
        if starts[mid] <= value:
            lo = mid + 1
        else:
            hi = mid
    return starts[lo - 1]


def _ceil_geq(starts, value: int) -> int:
    lo, hi = 0, len(starts)
    while lo < hi:
        mid = (lo + hi) // 2
        if starts[mid] < value:
            lo = mid + 1
        else:
            hi = mid
    return starts[min(lo, len(starts) - 1)]


def _cluster_index_at(old: index_mod.TextIndex, cp_boundary: int) -> int:
    """Cluster index whose start equals ``cp_boundary`` (it must be a start)."""
    return old.codepoint_to_cluster(cp_boundary, strict=True)


def _widen_safe_window(
    old: index_mod.TextIndex, k_start: int, k_end: int
) -> tuple[int, int]:
    """Extend a cluster window until its edges are segmentation-independent.

    Everything inside may be resegmented; nothing outside may then pair
    across an edge.  We guarantee that by walking outward across the
    context-dependent constructs of UAX #29:

    * RIS runs — pairing is parity based, so the whole run is included;
    * CR/LF/Control — singletons and the CRLF pair;
    * Prepend chains;
    * a trailing ZWJ that could join the following pictograph.
    """
    n = old.cluster_count
    if n == 0:
        return k_start, k_end

    # one-cluster look-ahead/behind on each side
    if k_start > 0:
        k_start -= 1
    if k_end < n:
        k_end += 1

    # --- left edge: extend across preceding context ------------------------
    changed = True
    while changed and k_start > 0:
        changed = False
        prev = old.cluster_text(k_start - 1)
        first = old.cluster_text(k_start)
        pfirst, lfirst = prev[0], first[0]
        g_prev_first = segmenter.gcb_group(pfirst)
        g_first_first = segmenter.gcb_group(lfirst)
        g_prev_last = segmenter.gcb_group(prev[-1])

        if g_prev_first == "PREPEND":
            k_start -= 1
            changed = True
            continue
        # whole contiguous RIS run must be inside (parity)
        if g_prev_first == "REGIONAL_INDICATOR" and \
                g_first_first == "REGIONAL_INDICATOR":
            k_start -= 1
            changed = True
            continue
        # CR × LF on the edge, or a control that forces a break on both sides
        if pfirst == "\r" and lfirst == "\n":
            k_start -= 1
            changed = True
            continue
        # previous cluster ends in ZWJ and might join first cluster;
        # previous cluster could join via Extend chain into first cluster
        if g_prev_last in ("ZWJ", "EXTEND", "SPACING_MARK"):
            k_start -= 1
            changed = True
            continue

    # --- right edge: extend across following context -----------------------
    changed = True
    while changed and k_end < n:
        changed = False
        last = old.cluster_text(k_end - 1)
        nxt = old.cluster_text(k_end)
        g_last_last = segmenter.gcb_group(last[-1])
        g_last_first = segmenter.gcb_group(last[0])
        g_next_first = segmenter.gcb_group(nxt[0])

        if g_next_first == "PREPEND":
            # a following Prepend can only bind to what follows IT, not back;
            # nothing to widen for Prepend on the right.
            break
        if g_last_first == "REGIONAL_INDICATOR" and \
                g_next_first == "REGIONAL_INDICATOR":
            k_end += 1
            changed = True
            continue
        if last[-1] == "\r" and nxt[0] == "\n":
            k_end += 1
            changed = True
            continue
        if g_last_last == "ZWJ":
            # ZWJ could join the following pictograph; include it.
            k_end += 1
            changed = True
            continue
    return k_start, k_end
