"""Structure-preserving three-way line merge.

Inputs are three texts: a common *base* and two independently edited
versions *local* and *remote*.  Both sides are diffed against the base
(see :mod:`merge3.diff`), yielding base-relative range edits, which are then
combined under the rules documented below.

Boundary semantics (the contract)
----------------------------------
1. **Disjoint edits auto-merge.**  Edits whose base ranges are disjoint
   half-open intervals are both taken.  Abutting ranges (one ends exactly
   where another begins) are disjoint and both taken.
2. **Insertions at a point.**  A pure insertion sits at a line boundary.
   Identical insertions at the same point from both sides are taken once.
   Different insertions at the same point are a ``same_point_insert``
   conflict; the caller picks local, remote, base (nothing), an explicit
   ordering of both, or explicit replacement text.
3. **Point vs. range interaction.**  An insertion whose boundary falls
   *strictly inside* the other side's changed range is an ``insert_range``
   conflict: its anchor disappeared into replaced content, so its position
   is genuinely undetermined.  An insertion at either *edge* of the range is
   not ambiguous — line inserts are anchored "before base line k", so it
   auto-merges with a deterministic order (the insert before the range leads
   when it shares the start boundary, and follows when it shares the end).
4. **Delete vs. modify.**  One side deleting exactly (or covering) what the
   other changes is a ``delete_modify`` conflict; choices are local, remote,
   base or explicit text.
5. **Divergent modifications.**  Identical base spans changed differently by
   the two sides are a ``divergent_modify`` conflict.
6. **Partial overlap.**  Ranges that overlap without one covering the other
   are a ``partial_overlap`` conflict; no proportional splicing is attempted.
7. **Identical changes are deduplicated** regardless of kind (two identical
   replacements or two identical whole-span deletions are taken once).
8. The core never emits conflict markers and never guesses: with one or
   more conflicts it returns no merged text.  Rebuilding requires an
   explicit, valid resolution for *every* conflict id.
9. Line terminators and a missing/present trailing newline are carried as
   exact characters; the merge only rewrites what the edits cover.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Optional

from .diff import Opcode, diff_edits
from .model import (
    ConflictBlock,
    ConflictType,
    Edit,
    MergeResult,
    Region,
)
from .textmodel import Line, boundary_offsets, split_lines


class MergeInputError(ValueError):
    """Raised when merge inputs fail structural validation."""


class ResolutionError(ValueError):
    """Raised when a conflict resolution set is incomplete or invalid."""


# --------------------------------------------------------------------------- #
# Boundary mapping: base line boundary -> side line boundary                  #
# --------------------------------------------------------------------------- #

@dataclass
class Alignment:
    """Side alignment bookkeeping for one derived edit set."""

    edits: list[Edit]
    base_lines: list[Line]
    side_lines: list[Line]
    side: str
    ops: list[Opcode]
    before: dict[int, int]  # base boundary -> side boundary, inserts excluded
    after: dict[int, int]   # base boundary -> side boundary, inserts included
    side_bounds: list[int]


def _build_alignment(base_text: str, side_text: str, side: str) -> Alignment:
    edits, base_lines, side_lines, ops = diff_edits(base_text, side_text, side)
    before = {0: 0}
    after = {0: 0}
    i = j = 0
    for op in ops:
        if op.kind == "insert":
            j += op.b1 - op.b0
            after[i] = j
        elif op.kind == "delete":
            for _ in range(op.a1 - op.a0):
                i += 1
                before[i] = j
                after[i] = j
        elif op.kind == "replace":
            # Both sides leave the hunk at the same boundary: advance both.
            i = op.a1
            j = op.b1
            before[i] = j
            after[i] = j
        else:  # equal
            for _ in range(op.a1 - op.a0):
                i += 1
                j += 1
                before[i] = j
                after[i] = j
    return Alignment(
        edits=edits,
        base_lines=base_lines,
        side_lines=side_lines,
        side=side,
        ops=ops,
        before=before,
        after=after,
        side_bounds=boundary_offsets(side_lines),
    )


# --------------------------------------------------------------------------- #
# Edit clustering                                                             #
# --------------------------------------------------------------------------- #

@dataclass
class Cluster:
    """A group of edits that interact (or one side's single edit)."""

    local: list[Edit] = field(default_factory=list)
    remote: list[Edit] = field(default_factory=list)

    @property
    def all_edits(self) -> list[Edit]:
        return self.local + self.remote

    @property
    def start(self) -> int:
        return min(e.start for e in self.all_edits)

    @property
    def end(self) -> int:
        return max(e.end for e in self.all_edits)

    @property
    def is_all_points(self) -> bool:
        return all(e.is_point for e in self.all_edits)


def _interact(e1: Edit, e2: Edit) -> bool:
    """True when *e1* and *e2* cannot both be applied blindly.

    Half-open ranges are disjoint when they merely abut (``[a,b)`` plus
    ``[b,c)`` do not interact), and a point edit at either *edge* of a range
    edit does not interact either: a line insert is anchored "before base
    line k", so its order relative to a range edit sharing an edge is fixed
    and assembly is deterministic.  A point strictly *inside* a changed range
    does interact (rule 3); two point edits interact when they share the
    point.
    """
    if e1.is_point and e2.is_point:
        return e1.start == e2.start
    if e1.is_point:
        return e2.start < e1.start < e2.end
    if e2.is_point:
        return e1.start < e2.start < e1.end
    return e1.start < e2.end and e2.start < e1.end


def _cluster_edits(local: list[Edit], remote: list[Edit]) -> list[Cluster]:
    """Group interacting edits; clusters come back in base-position order.

    Edits produced by one side's diff are mutually disjoint (a diff never
    emits overlapping ranges), so same-side edits only share a cluster when
    chained through an edit from the other side.
    """
    open_clusters: list[Cluster] = []

    def find_cluster(edit: Edit) -> Optional[Cluster]:
        hits = [c for c in open_clusters if any(_interact(edit, e) for e in c.all_edits)]
        if not hits:
            return None
        keep = hits[0]
        for extra in hits[1:]:
            keep.local.extend(extra.local)
            keep.remote.extend(extra.remote)
            open_clusters.remove(extra)
        return keep

    ordered = sorted(local + remote, key=lambda e: (e.start, e.end, e.side))
    finished: list[Cluster] = []
    for edit in ordered:
        cluster = find_cluster(edit)
        if cluster is None:
            cluster = Cluster()
            open_clusters.append(cluster)
        (cluster.local if edit.side == "local" else cluster.remote).append(edit)
        # Strict separation only: a later point landing exactly on c.end still
        # interacts (edge rule), so c.end == edit.start must not retire c.
        for c in list(open_clusters):
            if c is not cluster and c.end < edit.start:
                finished.append(c)
                open_clusters.remove(c)
    finished.extend(open_clusters)
    finished.sort(key=lambda c: (c.start, c.end))
    return finished


# --------------------------------------------------------------------------- #
# Materialization                                                             #
# --------------------------------------------------------------------------- #

def _materialize(base_sub: str, span_start: int, edits: list[Edit]) -> str:
    """Apply *edits* (all contained in the span) to a slice of the base.

    Parts of the span not touched by any edit remain exactly as in the base,
    terminators included.  Edits are applied back-to-front so earlier
    offsets stay valid; the input edits are disjoint per side except that a
    point insert can share a boundary with a neighboring range edit.  At a
    shared boundary the point insert is applied first (it *leads* the
    replaced content in forward order), which back-to-front means the range
    edit is applied before the point insert.
    """
    def order_key(e: Edit) -> tuple[int, int, int]:
        # Later start first; for equal start, the wider range goes first so
        # the point insert (zero width) is applied last = leads forward.
        return (e.start, e.end, 0 if e.is_point else 1)

    out = base_sub
    for edit in sorted(edits, key=order_key, reverse=True):
        rel_start = edit.start - span_start
        rel_end = edit.end - span_start
        out = out[:rel_start] + edit.replacement + out[rel_end:]
    return out


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


# --------------------------------------------------------------------------- #
# Merge engine                                                                #
# --------------------------------------------------------------------------- #

RANGE_CHOICES = ("local", "remote", "base", "custom_text")
POINT_CHOICES = (
    "local",
    "remote",
    "base",
    "local_then_remote",
    "remote_then_local",
    "custom_text",
)


class MergeEngine:
    """Run one three-way merge and rebuild text from explicit resolutions."""

    def __init__(self, base_text: str, local_text: str, remote_text: str,
                 request_id: str = "") -> None:
        for name, value in (("base_text", base_text),
                            ("local_text", local_text),
                            ("remote_text", remote_text)):
            if not isinstance(value, str):
                raise MergeInputError(f"{name} must be str, got {type(value).__name__}")
        self.base = base_text
        self.local = local_text
        self.remote = remote_text
        self.request_id = request_id
        self.local_aln = _build_alignment(base_text, local_text, "local")
        self.remote_aln = _build_alignment(base_text, remote_text, "remote")
        self.base_bounds = boundary_offsets(split_lines(base_text))
        self.clusters = _cluster_edits(self.local_aln.edits, self.remote_aln.edits)
        self.conflicts: list[ConflictBlock] = []
        self._conflict_by_cluster: dict[int, ConflictBlock] = {}
        self.decisions: list[dict] = []

    # -- coordinate helpers ------------------------------------------------- #

    def _boundary_index(self, offset: int) -> int:
        """Line-boundary index for an offset known to be a line boundary."""
        return self.base_bounds.index(offset)

    def _region(self, document: str, start: int, end: int,
                aln: Optional[Alignment] = None) -> Region:
        line_start = self._boundary_index(start)
        line_end = self._boundary_index(end)
        if aln is None:
            return Region(document, start, end, line_start, line_end)
        # Map onto the side using *pre-edit* anchors (``before``): a region
        # describes where the base material lived on that side before its
        # own edits applied.  A deleted span collapses to an empty point and
        # a pure insertion maps to the insertion boundary, both of which
        # slice the side document exactly for provenance display.
        sb0 = aln.before[line_start]
        sb1 = aln.before[line_end]
        return Region(document, aln.side_bounds[sb0], aln.side_bounds[sb1],
                      sb0, sb1)

    # -- main entry --------------------------------------------------------- #

    def run(self) -> MergeResult:
        auto_pieces: list[Optional[str]] = []
        for idx, cluster in enumerate(self.clusters):
            auto_pieces.append(self._evaluate_cluster(idx, cluster))

        if self.conflicts:
            merged_text: Optional[str] = None
            auto = False
        else:
            merged_text = self._assemble([p for p in auto_pieces])
            auto = True

        return MergeResult(
            merged_text=merged_text,
            conflicts=self.conflicts,
            local_edits=list(self.local_aln.edits),
            remote_edits=list(self.remote_aln.edits),
            decisions=self.decisions,
            auto_merged=auto,
            request_id=self.request_id,
        )

    def _evaluate_cluster(self, idx: int, cluster: Cluster) -> Optional[str]:
        """Return auto-merged replacement for the cluster, or register conflict."""
        s, e = cluster.start, cluster.end
        base_sub = self.base[s:e]
        local_view = _materialize(base_sub, s, cluster.local)
        remote_view = _materialize(base_sub, s, cluster.remote)

        # Single-side cluster: the other side made no change here.
        if not cluster.local or not cluster.remote:
            side = "local" if cluster.local else "remote"
            view = local_view if cluster.local else remote_view
            self._record(cluster, "accepted", "single_side_edit",
                         f"only {side} touches base [{s},{e}); applied verbatim")
            return view

        # Both sides: same-point insertions.
        if cluster.is_all_points:
            local_ins = "".join(ed.replacement for ed in cluster.local)
            remote_ins = "".join(ed.replacement for ed in cluster.remote)
            if local_ins == remote_ins:
                self._record(cluster, "accepted", "identical_insert",
                             f"both sides insert identical {len(local_ins)} chars "
                             f"at point {s}; taken once")
                return local_ins
            block = self._conflict(
                cluster, ConflictType.SAME_POINT_INSERT,
                base_sub, local_view, remote_view, POINT_CHOICES)
            self._record(cluster, "conflict", block.conflict_type.value,
                         f"different insertions at point {s}: "
                         f"{len(local_ins)} chars vs {len(remote_ins)} chars")
            self._conflict_by_cluster[idx] = block
            return None

        # Both sides, at least one range edit.
        if local_view == remote_view:
            self._record(cluster, "accepted", "identical_change",
                         f"both sides produce identical text over [{s},{e}); "
                         f"taken once")
            return local_view

        local_has_point = any(ed.is_point for ed in cluster.local)
        remote_has_point = any(ed.is_point for ed in cluster.remote)
        if local_has_point or remote_has_point:
            ctype = ConflictType.INSERT_RANGE
            reason = ("insertion at a boundary inside/at edge of the other "
                      "side's changed range; ordering is the caller's choice")
        else:
            ctype, reason = self._range_conflict_type(cluster, local_view, remote_view)

        block = self._conflict(cluster, ctype, base_sub, local_view, remote_view,
                               RANGE_CHOICES)
        self._record(cluster, "conflict", ctype.value, reason)
        self._conflict_by_cluster[idx] = block
        return None

    @staticmethod
    def _range_conflict_type(cluster: Cluster, local_view: str,
                             remote_view: str) -> tuple[ConflictType, str]:
        ls = min(ed.start for ed in cluster.local)
        le = max(ed.end for ed in cluster.local)
        rs = min(ed.start for ed in cluster.remote)
        re_ = max(ed.end for ed in cluster.remote)
        local_covers = ls <= rs and re_ <= le
        remote_covers = rs <= ls and le <= re_
        # A side "deletes the span" when its view of the whole union is empty.
        if local_view == "" and local_covers:
            return (ConflictType.DELETE_MODIFY,
                    "local deletes the span that remote modifies")
        if remote_view == "" and remote_covers:
            return (ConflictType.DELETE_MODIFY,
                    "remote deletes the span that local modifies")
        if ls == rs and le == re_:
            return (ConflictType.DIVERGENT_MODIFY,
                    f"both sides modify identical span [{ls},{le}) differently")
        return (ConflictType.PARTIAL_OVERLAP,
                f"changed spans [{ls},{le}) and [{rs},{re_}) overlap without "
                "coinciding; no proportional splicing attempted")

    # -- conflict construction --------------------------------------------- #

    def _conflict(self, cluster: Cluster, ctype: ConflictType,
                  base_text: str, local_text: str, remote_text: str,
                  choices: tuple[str, ...]) -> ConflictBlock:
        s, e = cluster.start, cluster.end
        cid = f"c{len(self.conflicts) + 1}"
        block = ConflictBlock(
            conflict_id=cid,
            conflict_type=ctype,
            base_region=self._region("base", s, e),
            local_region=self._region("local", s, e, self.local_aln),
            remote_region=self._region("remote", s, e, self.remote_aln),
            base_text=base_text,
            local_text=local_text,
            remote_text=remote_text,
            local_edit_ids=tuple(ed.edit_id for ed in cluster.local),
            remote_edit_ids=tuple(ed.edit_id for ed in cluster.remote),
            allowed_resolutions=choices,
        )
        self.conflicts.append(block)
        return block

    # -- diagnostics records ------------------------------------------------ #

    def _record(self, cluster: Cluster, outcome: str, category: str,
                detail: str) -> None:
        s, e = cluster.start, cluster.end
        self.decisions.append({
            "request_id": self.request_id,
            "cluster_span": [s, e],
            "outcome": outcome,       # accepted | conflict
            "category": category,
            "detail": detail,
            "local_edit_ids": [ed.edit_id for ed in cluster.local],
            "remote_edit_ids": [ed.edit_id for ed in cluster.remote],
            "local_edit_spans": [[ed.start, ed.end] for ed in cluster.local],
            "remote_edit_spans": [[ed.start, ed.end] for ed in cluster.remote],
            "local_sha256_12": _digest(
                "".join(ed.replacement for ed in cluster.local)),
            "remote_sha256_12": _digest(
                "".join(ed.replacement for ed in cluster.remote)),
            "local_chars": sum(len(ed.replacement) for ed in cluster.local),
            "remote_chars": sum(len(ed.replacement) for ed in cluster.remote),
        })

    # -- assembly & rebuild ------------------------------------------------- #

    def _assemble(self, cluster_outputs: list[Optional[str]]) -> str:
        parts: list[str] = []
        cursor = 0
        for cluster, output in zip(self.clusters, cluster_outputs):
            parts.append(self.base[cursor:cluster.start])
            if output is None:
                raise ResolutionError(
                    "cannot assemble: unresolved conflict present")
            parts.append(output)
            cursor = cluster.end
        parts.append(self.base[cursor:])
        return "".join(parts)

    def rebuild(self, result: MergeResult,
                resolutions: dict[str, dict]) -> str:
        """Rebuild merged text given an explicit resolution per conflict.

        *resolutions* maps ``conflict_id`` to ``{"choice": ..., "text": ...}``
        where ``text`` is required only for ``custom_text`` and forbidden
        otherwise.  Every conflict must be resolved; unknown ids or choices
        raise :class:`ResolutionError` listing exactly what was wrong.
        """
        if result is not None and result.conflicts is not self.conflicts:
            # Keep callers honest: rebuild the engine's own run.
            if {c.conflict_id for c in result.conflicts} != {
                c.conflict_id for c in self.conflicts
            }:
                raise ResolutionError("result does not belong to this engine run")

        wanted = {c.conflict_id: c for c in self.conflicts}
        unknown = set(resolutions) - set(wanted)
        if unknown:
            raise ResolutionError(
                f"resolutions for unknown conflict ids: {sorted(unknown)}")
        missing = sorted(set(wanted) - set(resolutions),
                         key=lambda cid: int(cid[1:]))
        if missing:
            raise ResolutionError(f"missing resolutions for: {missing}")

        outputs: list[Optional[str]] = []
        for idx, cluster in enumerate(self.clusters):
            block = self._conflict_by_cluster.get(idx)
            if block is None:
                s, e = cluster.start, cluster.end
                base_sub = self.base[s:e]
                if cluster.local:
                    outputs.append(_materialize(base_sub, s, cluster.local))
                else:
                    outputs.append(_materialize(base_sub, s, cluster.remote))
                continue

            spec = resolutions[block.conflict_id]
            choice = spec.get("choice")
            custom = spec.get("text")
            allowed = set(block.allowed_resolutions)
            if choice not in allowed:
                raise ResolutionError(
                    f"{block.conflict_id}: choice {choice!r} not allowed; "
                    f"choose one of {list(block.allowed_resolutions)}")
            if choice == "custom_text":
                if not isinstance(custom, str):
                    raise ResolutionError(
                        f"{block.conflict_id}: custom_text requires string 'text'")
                outputs.append(custom)
                continue
            if custom is not None:
                raise ResolutionError(
                    f"{block.conflict_id}: 'text' only valid with custom_text")

            if block.conflict_type == ConflictType.SAME_POINT_INSERT:
                local_ins = "".join(ed.replacement for ed in cluster.local)
                remote_ins = "".join(ed.replacement for ed in cluster.remote)
                outputs.append({
                    "local": local_ins,
                    "remote": remote_ins,
                    "base": "",
                    "local_then_remote": local_ins + remote_ins,
                    "remote_then_local": remote_ins + local_ins,
                }[choice])
            else:
                s, e = cluster.start, cluster.end
                base_sub = self.base[s:e]
                outputs.append({
                    "local": _materialize(base_sub, s, cluster.local),
                    "remote": _materialize(base_sub, s, cluster.remote),
                    "base": base_sub,
                }[choice])

        merged = self._assemble(outputs)
        # Record how each conflict was settled.
        for cid, spec in resolutions.items():
            self.decisions.append({
                "request_id": self.request_id,
                "conflict_id": cid,
                "outcome": "resolved",
                "choice": spec.get("choice"),
                "custom_chars": len(spec["text"]) if spec.get("choice") ==
                "custom_text" else 0,
            })
        return merged


def three_way_merge(base_text: str, local_text: str, remote_text: str,
                    request_id: str = "") -> tuple[MergeEngine, MergeResult]:
    """Convenience entry point: construct, run, return ``(engine, result)``."""
    engine = MergeEngine(base_text, local_text, remote_text, request_id)
    return engine, engine.run()
