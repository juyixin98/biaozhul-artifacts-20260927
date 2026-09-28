"""Canonical target graph — the heart of the security kernel.

The graph is built *entirely before any byte is extracted* from the parsed
evidence. It answers three questions:

1. **Canonical path** — where inside the isolated root does this entry live?
   Rejects traversal (``..``), absolute/drive-lettered and control-char names.
2. **Physical target** — after resolving the archive's own symlinks purely
   lexically (no disk syscalls, no TOCTOU), where would payload bytes land?
   Rejects escapes, loops, file/type conflicts and alias collisions.
3. **Budget** — total bytes, per-file size, entry count, nesting depth and
   declared compression ratio.

Because regular files are written to their *resolved physical* target through
O_CREAT|O_EXCL|O_NOFOLLOW, an in-archive symlink can never redirect a write
outside the isolation root.
"""
from __future__ import annotations

import posixpath
from dataclasses import dataclass, field

from ..config import Budgets, Policy
from .errors import (
    BudgetDepthExceeded,
    BudgetEntryCountExceeded,
    BudgetFileSizeExceeded,
    BudgetTotalSizeExceeded,
    CaseCollision,
    CompressionBomb,
    DuplicateName,
    HardlinkRejected,
    PathEscape,
    SpecialFileRejected,
    SymlinkAlias,
    SymlinkEscape,
    SymlinkLoop,
    SymlinkRejected,
    TypeConflict,
    UnsafeName,
)
from .model import EntryEvidence, EntryKind

TuplePath = tuple[str, ...]


@dataclass
class PlannedAction:
    """One ordered extraction action (all paths relative to the run root)."""

    kind: str  # "mkdir" | "symlink" | "write"
    canonical: TuplePath
    physical: TuplePath | None  # write destination (resolved); None for symlink
    mode: int
    link_target: str | None = None
    declared_size: int = 0
    compress_size: int | None = None
    compress_type: int | None = None
    evidence_index: int = -1
    opener: object = None  # set lazily by the service layer (avoid pickling)


@dataclass
class PlanStats:
    entries: int = 0
    files: int = 0
    directories: int = 0
    symlinks: int = 0
    total_declared_bytes: int = 0
    compressed_bytes: int = 0
    worst_compression_ratio: float = 0.0
    max_depth_seen: int = 0


@dataclass
class Plan:
    container: str
    actions: list[PlannedAction] = field(default_factory=list)
    stats: PlanStats = field(default_factory=PlanStats)
    steps: list[str] = field(default_factory=list)
    # canonical tuple -> action, for inspection/audit
    files: dict[TuplePath, PlannedAction] = field(default_factory=dict)
    symlinks: dict[TuplePath, str] = field(default_factory=dict)
    directories: set[TuplePath] = field(default_factory=set)


# ---------------------------------------------------------------------------
# Name normalization — pure, lexical, POSIX semantics for archive members.
# ---------------------------------------------------------------------------
def normalize_name(raw: str) -> TuplePath:
    """Convert an archive member name into a strict in-root component tuple.

    Raises PathEscape / UnsafeName for anything that could leave the root or
    carry ambiguous/control characters.
    """
    if raw is None or raw == "":
        raise UnsafeName("empty entry name", evidence=repr(raw))
    # Reject control characters and NUL; reject backslash (not a POSIX separator
    # here, so accepting it would create surprising names on Windows targets).
    if "\x00" in raw:
        raise UnsafeName("NUL byte in entry name", evidence=repr(raw))
    if any(ord(c) < 32 for c in raw):
        raise UnsafeName("control character in entry name", evidence=repr(raw))
    if "\\" in raw:
        raise UnsafeName("backslash is not allowed in archive member names", evidence=repr(raw))

    # Absolute POSIX and Windows drive / UNC names are escapes regardless of
    # later normalization.
    if raw.startswith("/"):
        raise PathEscape("absolute path is not allowed", evidence=raw)
    if len(raw) >= 2 and raw[1] == ":":
        raise PathEscape("drive-letter path is not allowed", evidence=raw)
    if raw.startswith("//") or raw.startswith("?"):
        raise PathEscape("UNC / NT-namespaced path is not allowed", evidence=raw)

    normalized = posixpath.normpath(raw)
    if normalized in (".", ""):
        # An entry naming the root itself is harmless but useless; reject as
        # unsafe/ambiguous rather than creating a zero-component target.
        raise UnsafeName("entry resolves to the archive root", evidence=raw)
    if normalized.startswith("../") or normalized == "..":
        raise PathEscape("path traversal outside the extraction root", evidence=raw)

    parts = tuple(normalized.split("/"))
    if any(p in ("", ".", "..") for p in parts):
        # normpath should have collapsed these; fail closed if anything remains.
        raise PathEscape("non-canonical component after normalization", evidence=raw)
    return parts


def split_dirname(raw: str) -> TuplePath:
    """Normalize a directory entry name, dropping a trailing slash."""
    return normalize_name(raw.rstrip("/"))


# ---------------------------------------------------------------------------
# Virtual symlink resolution (POSIX pathname semantics, lexical only).
# ---------------------------------------------------------------------------
def resolve(
    start: TuplePath,
    symlinks: dict[TuplePath, TuplePath],
    *,
    steps_budget: int,
    evidence_name: str,
) -> TuplePath:
    """Resolve ``start`` against the in-archive symlink map, component-wise.

    POSIX pathname-resolution semantics, evaluated purely lexically (no disk
    syscalls, so no TOCTOU): when a component names a symlink, the *raw*
    components of the symlink's target are prepended to the remaining work and
    the symlink component is NOT appended — a subsequent ``..`` therefore pops
    the symlink's parent directory exactly as the kernel would do. A ``..`` that
    would rise above the root raises SymlinkEscape.

    ``symlinks`` maps a link's canonical location to the raw components of its
    declared target (e.g. ("..", "store")), validated only for syntax.

    Raises SymlinkEscape / SymlinkLoop on violations.
    """
    work: list[str] = list(start)
    resolved: list[str] = []
    entered: set[TuplePath] = set()
    steps = 0

    while work:
        comp = work.pop(0)
        steps += 1
        if steps > steps_budget:
            raise SymlinkLoop(
                f"symlink resolution exceeded {steps_budget} steps",
                evidence=evidence_name,
            )
        if comp == ".":
            continue
        if comp == "..":
            if not resolved:
                raise SymlinkEscape(
                    "resolution rises above the extraction root",
                    evidence=evidence_name,
                )
            resolved.pop()
            continue

        candidate = tuple(resolved + [comp])
        target = symlinks.get(candidate)
        if target is not None:
            if candidate in entered:
                raise SymlinkLoop(
                    f"symlink chain loops at {'/'.join(candidate)!r}",
                    evidence=evidence_name,
                )
            entered.add(candidate)
            # Textual substitution: raw target components replace this
            # component; its parent stays on the stack (POSIX behavior).
            work = list(target) + work
        else:
            resolved.append(comp)

    return tuple(resolved)


def _link_raw_components(raw_target: str, link_location: TuplePath) -> TuplePath:
    """Validate a symlink target syntactically and return its raw components.

    The components are NOT collapsed against the link directory — POSIX does
    that during pathname resolution (handled by :func:`resolve`), so a later
    ``..`` behaves correctly.
    """
    if raw_target == "":
        raise SymlinkEscape("empty symlink target", evidence="/".join(link_location))
    if "\x00" in raw_target:
        raise UnsafeName("NUL byte in symlink target", evidence=repr(raw_target))
    if raw_target.startswith("/"):
        raise SymlinkEscape("symlink target is absolute", evidence=raw_target)
    if len(raw_target) >= 2 and raw_target[1] == ":":
        raise SymlinkEscape("drive-letter symlink target", evidence=raw_target)
    parts = tuple(p for p in raw_target.split("/") if p not in ("", "."))
    if not parts:
        raise SymlinkEscape("symlink target has no path components", evidence=raw_target)
    return parts


# ---------------------------------------------------------------------------
# Graph construction
# ---------------------------------------------------------------------------
def build_plan(
    container: str,
    evidence_list: list[EntryEvidence],
    openers: list,
    *,
    budgets: Budgets,
    policy: Policy,
    compressed_bytes: int,
) -> Plan:
    plan = Plan(container=container)
    steps = plan.steps
    steps.append(f"build_plan: {len(evidence_list)} entries, container={container}")

    if len(evidence_list) > budgets.max_entries:
        raise BudgetEntryCountExceeded(
            f"entry count {len(evidence_list)} exceeds budget {budgets.max_entries}",
            evidence=f"count={len(evidence_list)}",
        )

    # --- Pass 1: normalize names, reject kinds, collect canonical claims. ----
    # canonical_lower -> canonical exact, for case-collision detection.
    lower_map: dict[str, TuplePath] = {}
    raw_entries: list[tuple[EntryEvidence, TuplePath, TuplePath | None]] = []
    # symlink raw tuple targets resolved relative to link dir (membership unknown yet).
    symlink_targets: dict[TuplePath, TuplePath] = {}

    total_declared = 0
    max_depth = 0

    for ev in evidence_list:
        steps.append(
            f"entry#{ev.index} kind={ev.kind.value} name={ev.raw_name!r} "
            f"declared={ev.declared_size}B"
        )
        if ev.kind == EntryKind.HARDLINK:
            raise HardlinkRejected(
                "hard links are explicitly rejected",
                evidence=ev.raw_name,
            )
        if ev.kind == EntryKind.SPECIAL:
            raise SpecialFileRejected(
                "special files (fifo/device/char/block) are explicitly rejected",
                evidence=ev.raw_name,
            )
        if ev.kind == EntryKind.SYMLINK and not policy.allow_symlinks:
            raise SymlinkRejected(
                "symlinks disabled by policy", evidence=ev.raw_name
            )

        if ev.kind == EntryKind.DIRECTORY:
            canonical = split_dirname(ev.raw_name)
        else:
            canonical = normalize_name(ev.raw_name)

        depth = len(canonical)
        max_depth = max(max_depth, depth)
        if depth > budgets.max_depth:
            raise BudgetDepthExceeded(
                f"entry depth {depth} exceeds budget {budgets.max_depth}",
                evidence=f"{ev.raw_name} depth={depth}",
            )

        # Exact duplicate / same-name overwrite.
        key_lower = "/".join(p.casefold() for p in canonical)
        if key_lower in lower_map:
            prior = lower_map[key_lower]
            if prior == canonical:
                raise DuplicateName(
                    "duplicate entry name (same-name overwrite) is not allowed",
                    evidence=f"{ev.raw_name} (dup at #{ev.index})",
                )
            if not policy.allow_case_collisions:
                raise CaseCollision(
                    f"case-insensitive collision with {'/'.join(prior)!r}",
                    evidence=ev.raw_name,
                )
        lower_map[key_lower] = canonical

        if ev.kind == EntryKind.FILE:
            if ev.declared_size < 0:
                raise UnsafeName("negative declared size", evidence=ev.raw_name)
            if ev.declared_size > budgets.max_file_size_bytes:
                raise BudgetFileSizeExceeded(
                    f"declared file size {ev.declared_size} exceeds per-file "
                    f"budget {budgets.max_file_size_bytes}",
                    evidence=f"{ev.raw_name} size={ev.declared_size}",
                )
            total_declared += ev.declared_size
            if total_declared > budgets.max_total_uncompressed_bytes:
                raise BudgetTotalSizeExceeded(
                    f"declared total {total_declared} exceeds budget "
                    f"{budgets.max_total_uncompressed_bytes}",
                    evidence=f"{ev.raw_name} cumulative={total_declared}",
                )
        elif ev.kind == EntryKind.SYMLINK:
            target_tuple = _link_raw_components(ev.link_target or "", canonical)
            symlink_targets[canonical] = target_tuple
        raw_entries.append((ev, canonical, None))

    # --- Pass 2: resolve physical targets component-wise. -------------------
    # A two-phase resolve map: symlink canonical -> tuple target. Membership is
    # evaluated during resolution; this also detects loops and prefix conflicts.
    symlink_set = set(symlink_targets.keys())
    file_set: set[TuplePath] = {c for ev, c, _ in raw_entries if ev.kind == EntryKind.FILE}
    dir_set: set[TuplePath] = {c for ev, c, _ in raw_entries if ev.kind == EntryKind.DIRECTORY}

    def physical_for(canonical: TuplePath, evidence_name: str) -> TuplePath:
        return resolve(
            canonical,
            symlink_targets,
            steps_budget=budgets.symlink_resolution_steps,
            evidence_name=evidence_name,
        )

    # Validate every symlink by resolving its own target relative to its
    # directory (dangling final targets are allowed, but escaping the root or
    # looping is rejected here). File paths through links are validated again
    # when their physical targets are computed below.
    for link_loc, target in symlink_targets.items():
        steps.append(
            f"resolve symlink {'/'.join(link_loc)!r} -> "
            f"{'/'.join(target)!r}"
        )
        start = tuple(link_loc[:-1]) + tuple(target)
        resolve(
            start,
            symlink_targets,
            steps_budget=budgets.symlink_resolution_steps,
            evidence_name="/".join(link_loc),
        )

    # Prefix type conflicts: a file cannot be a directory prefix of something
    # else, and two entries of different kinds cannot share a canonical path.
    all_canonical: dict[TuplePath, EntryKind] = {c: ev.kind for ev, c, _ in raw_entries}
    for canonical, kind in all_canonical.items():
        for prefix_len in range(1, len(canonical)):
            prefix = canonical[:prefix_len]
            owner = all_canonical.get(prefix)
            if owner == EntryKind.FILE:
                raise TypeConflict(
                    f"{'/'.join(canonical)!r} is nested under a regular file "
                    f"{'/'.join(prefix)!r}",
                    evidence=f"{'/'.join(prefix)} (file) vs {ev_label(all_canonical, canonical)}",
                )
            if owner == EntryKind.SYMLINK:
                # A symlink occupying a path prefix: traversal resolves through
                # it. That is legal only if resolution stays in root; it does
                # not conflict by itself, but a symlink + explicit directory at
                # the same path was caught as exact dup.
                pass
            # An implicit prefix under a symlink is resolved virtually.

    # Physical placement map: disk path -> (canonical, kind). Files land at
    # resolved physical targets; symlinks/dirs land at canonical. Two distinct
    # canonical names must never claim the same disk path.
    placements: dict[TuplePath, tuple[TuplePath, str]] = {}

    def claim_placement(disk: TuplePath, canonical: TuplePath, kind: str, evidence_name: str) -> None:
        existing = placements.get(disk)
        if existing is not None:
            prior_canonical, prior_kind = existing
            if prior_kind == kind:
                raise SymlinkAlias(
                    f"entries {'/'.join(prior_canonical)!r} and {evidence_name!r} "
                    f"resolve to the same physical path {'/'.join(disk)!r}",
                    evidence=f"{evidence_name} == {'/'.join(prior_canonical)}",
                )
            raise TypeConflict(
                f"physical path {'/'.join(disk)!r} claimed as {prior_kind} and {kind}",
                evidence=f"{evidence_name} vs {'/'.join(prior_canonical)}",
            )
        placements[disk] = (canonical, kind)

    # First claim symlinks and directories at canonical, so file collisions are
    # diagnosed against link targets precisely.
    for ev, canonical, _ in raw_entries:
        if ev.kind == EntryKind.SYMLINK:
            claim_placement(canonical, canonical, "symlink", ev.raw_name)
        elif ev.kind == EntryKind.DIRECTORY:
            claim_placement(canonical, canonical, "directory", ev.raw_name)

    actions: list[PlannedAction] = []
    physical_for_files: dict[TuplePath, TuplePath] = {}
    dirs_to_create: set[TuplePath] = set()

    for idx, (ev, canonical, _) in enumerate(raw_entries):
        if ev.kind == EntryKind.FILE:
            physical = physical_for(canonical, ev.raw_name)
            physical_for_files[canonical] = physical
            steps.append(
                f"map file {ev.raw_name!r} canonical=/{'/'.join(canonical)} "
                f"physical=/{'/'.join(physical)}"
            )
            claim_placement(physical, canonical, "file", ev.raw_name)

            # Every component of the physical path must not be an existing file.
            for prefix_len in range(1, len(physical)):
                prefix = physical[:prefix_len]
                if prefix in file_set:
                    raise TypeConflict(
                        f"physical parent {'/'.join(prefix)!r} is a regular file",
                        evidence=ev.raw_name,
                    )
            # Parent directories (fully resolved) must exist as real dirs;
            # collect every physical prefix that is not itself a symlink.
            for prefix_len in range(1, len(physical) + 1):
                prefix = physical[:prefix_len]
                if prefix in symlink_set:
                    continue  # provided virtually by a symlink action
                if prefix == physical:
                    continue  # the file itself
                dirs_to_create.add(prefix)
        elif ev.kind == EntryKind.DIRECTORY:
            for prefix_len in range(1, len(canonical) + 1):
                prefix = canonical[:prefix_len]
                if prefix in symlink_set:
                    continue
                dirs_to_create.add(prefix)

    # Ensure all collected directories are real (not files/symlinks that were
    # not explicit). dirs_to_create may include a prefix equal to a symlink
    # location — skip those; resolution proved traversal stays in root.
    for d in sorted(dirs_to_create):
        if d in symlink_set:
            continue
        owner = all_canonical.get(d)
        if owner == EntryKind.FILE:
            raise TypeConflict(
                f"required directory {'/'.join(d)!r} is a regular file",
                evidence=f"{'/'.join(d)}",
            )
        if owner == EntryKind.SYMLINK:
            continue
        plan.directories.add(d)

    # --- Pass 3: ordered actions. -------------------------------------------
    # mkdir first (depth order), then symlinks, then file writes.
    for d in sorted(plan.directories, key=lambda t: (len(t), t)):
        actions.append(
            PlannedAction(
                kind="mkdir",
                canonical=d,
                physical=d,
                mode=0o755,
            )
        )

    opener_by_index = {ev.index: openers[i] for i, (ev, _, _) in enumerate(raw_entries)}
    evidence_by_index = {ev.index: ev for ev, _, _ in raw_entries}
    for ev, canonical, _ in raw_entries:
        if ev.kind == EntryKind.SYMLINK:
            actions.append(
                PlannedAction(
                    kind="symlink",
                    canonical=canonical,
                    physical=None,
                    mode=ev.mode,
                    link_target=ev.link_target,
                    evidence_index=ev.index,
                )
            )

    for ev, canonical, _ in raw_entries:
        if ev.kind == EntryKind.FILE:
            physical = physical_for_files[canonical]
            actions.append(
                PlannedAction(
                    kind="write",
                    canonical=canonical,
                    physical=physical,
                    mode=ev.mode & 0o777 or 0o644,
                    declared_size=ev.declared_size,
                    compress_size=ev.compress_size,
                    compress_type=ev.compress_type,
                    evidence_index=ev.index,
                    opener=opener_by_index[ev.index],
                )
            )
            plan.files[canonical] = actions[-1]
        elif ev.kind == EntryKind.SYMLINK:
            plan.symlinks[canonical] = ev.link_target or ""

    # --- Stats and compression-bomb check (declared headers only). ----------
    stats = plan.stats
    stats.entries = len(raw_entries)
    stats.files = len(plan.files)
    stats.directories = len(plan.directories)
    stats.symlinks = len(plan.symlinks)
    stats.total_declared_bytes = total_declared
    stats.compressed_bytes = compressed_bytes
    stats.max_depth_seen = max_depth

    # Declared ratio is recorded for diagnostics only. A header *claiming* a
    # huge uncompressed size with a few stored bytes may simply be a lying
    # header (that case is caught precisely as declared_length_mismatch while
    # streaming); rejecting on the claim alone would mis-classify it. The hard
    # bomb defence is the total/per-file size budgets plus an exact ratio check
    # against the bytes actually decompressed (see extractor.execute_plan).
    if compressed_bytes > 0 and total_declared > 0:
        stats.worst_compression_ratio = total_declared / compressed_bytes
    steps.append(
        f"plan accepted: files={stats.files} dirs={stats.directories} "
        f"symlinks={stats.symlinks} declared={total_declared}B "
        f"stored={compressed_bytes}B declared_ratio={stats.worst_compression_ratio:.2f}x"
    )

    plan.actions = actions
    return plan


def ev_label(kind_map: dict, canonical: TuplePath) -> str:
    owner = kind_map.get(canonical)
    return owner.value if owner is not None else "/".join(canonical)
