"""Canonical target-path graph — the security core.

For every archive entry we build, *before any byte is written*, a graph of
canonical paths inside an abstract output root and prove that:

* no name escapes the root (absolute, drive, ``..``)
* no two materialized paths collide case-insensitively and no entry duplicates
  another exactly
* no file/symlink is declared under another non-directory entry
* every symbolic link points inside the root, resolves without a cycle and is
  not dangling
* nesting depth stays within budget

Resolution is purely lexical/virtual (the real filesystem is never consulted
during planning), so a hostile archive cannot influence decisions through the
host directory tree.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum

from .archiveio import Entry, EntryKind
from .budget import Budget, Usage
from .errors import RejectionCategory, RejectionError

_DRIVE_RE = re.compile(r"^[A-Za-z]:")
_MAX_LINK_TARGET_BYTES = 4096


class NodeKind(str, Enum):
    DIR = "directory"
    FILE = "file"
    SYMLINK = "symlink"


@dataclass
class Node:
    parts: tuple[str, ...]
    raw: str
    kind: NodeKind
    index: int
    size: int = 0
    mode: int = 0o644
    link_text: str | None = None
    implicit: bool = False

    @property
    def relpath(self) -> str:
        return "/".join(self.parts)


@dataclass
class Plan:
    nodes: dict[tuple[str, ...], Node] = field(default_factory=dict)
    explicit: set[tuple[str, ...]] = field(default_factory=set)
    order: list[tuple[str, ...]] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    link_targets: dict[tuple[str, ...], tuple[str, ...]] = field(default_factory=dict)

    def directories(self) -> list[Node]:
        return [
            self.nodes[p]
            for p in sorted(self.nodes, key=lambda x: (len(x), x))
            if self.nodes[p].kind is NodeKind.DIR
        ]

    def symlinks(self) -> list[Node]:
        return [self.nodes[p] for p in self.order if self.nodes[p].kind is NodeKind.SYMLINK]

    def files(self) -> list[Node]:
        return [self.nodes[p] for p in self.order if self.nodes[p].kind is NodeKind.FILE]


# --------------------------------------------------------------------------- #
# Name normalization
# --------------------------------------------------------------------------- #

def normalize_name(raw: str) -> tuple[str, ...]:
    """Normalize an archive member name into canonical root-relative parts."""
    if raw == "":
        raise RejectionError(
            RejectionCategory.ARCHIVE_CORRUPT, "entry with empty name", entry=raw
        )
    if "\x00" in raw:
        raise RejectionError(
            RejectionCategory.PATH_INVALID,
            "entry name contains a NUL byte",
            entry=raw,
        )
    if "\\" in raw:
        # Backslashes are never separators in our POSIX target tree.
        raise RejectionError(
            RejectionCategory.PATH_INVALID,
            "entry name contains a backslash",
            entry=raw,
        )
    if raw.startswith("/"):
        raise RejectionError(
            RejectionCategory.PATH_TRAVERSAL,
            "absolute entry name is not allowed",
            entry=raw,
        )
    if _DRIVE_RE.match(raw):
        raise RejectionError(
            RejectionCategory.PATH_TRAVERSAL,
            "drive-letter entry name is not allowed",
            entry=raw,
        )

    parts: list[str] = []
    for component in raw.split("/"):
        if component in ("", "."):
            continue
        if component == "..":
            raise RejectionError(
                RejectionCategory.PATH_TRAVERSAL,
                "entry name contains a parent-directory component",
                entry=raw,
            )
        parts.append(component)
    return tuple(parts)


def normalize_link_target(raw_target: str, link_parts: tuple[str, ...]) -> list[str]:
    """Lexically normalize a symlink target and prove it stays in-root."""
    link_name = "/".join(link_parts)
    if raw_target == "":
        raise RejectionError(
            RejectionCategory.SYMLINK_ESCAPE,
            "empty symlink target",
            entry=link_name,
        )
    if "\x00" in raw_target:
        raise RejectionError(
            RejectionCategory.SYMLINK_ESCAPE,
            "symlink target contains a NUL byte",
            entry=link_name,
        )
    if "\\" in raw_target:
        raise RejectionError(
            RejectionCategory.SYMLINK_ESCAPE,
            "symlink target contains a backslash",
            entry=link_name,
        )
    if raw_target.startswith("/"):
        raise RejectionError(
            RejectionCategory.SYMLINK_ESCAPE,
            "absolute symlink target is not allowed",
            entry=link_name,
        )
    if _DRIVE_RE.match(raw_target):
        raise RejectionError(
            RejectionCategory.SYMLINK_ESCAPE,
            "drive-letter symlink target is not allowed",
            entry=link_name,
        )

    cur = list(link_parts[:-1])
    for component in raw_target.split("/"):
        if component in ("", "."):
            continue
        if component == "..":
            if not cur:
                raise RejectionError(
                    RejectionCategory.SYMLINK_ESCAPE,
                    f"symlink target {raw_target!r} escapes the output root",
                    entry=link_name,
                )
            cur.pop()
        else:
            cur.append(component)
    return cur


# --------------------------------------------------------------------------- #
# Graph construction
# --------------------------------------------------------------------------- #

class Planner:
    def __init__(self, budget: Budget) -> None:
        self.budget = budget
        self.plan = Plan()
        # folded(parts) -> canonical parts; covers BOTH explicit and implicit
        # nodes because implicit directories are materialized on disk too.
        self._fold_map: dict[tuple[str, ...], tuple[str, ...]] = {}

    def add(self, entry: Entry, link_text: str | None = None) -> None:
        if entry.kind is EntryKind.HARDLINK:
            raise RejectionError(
                RejectionCategory.HARDLINK,
                "hard link entries are not allowed",
                entry=entry.name,
            )
        if entry.kind is EntryKind.SPECIAL:
            raise RejectionError(
                RejectionCategory.ENTRY_SPECIAL,
                "special files (fifo/device/socket) are not allowed",
                entry=entry.name,
            )

        parts = normalize_name(entry.name)
        if not parts:
            # "." / "./" naming the archive root itself: harmless for a dir.
            if entry.kind is EntryKind.DIRECTORY:
                return
            raise RejectionError(
                RejectionCategory.PATH_INVALID,
                "entry name resolves to the archive root",
                entry=entry.name,
            )

        self.budget.check_depth(len(parts), entry.name)

        kind = {
            EntryKind.DIRECTORY: NodeKind.DIR,
            EntryKind.FILE: NodeKind.FILE,
            EntryKind.SYMLINK: NodeKind.SYMLINK,
        }[entry.kind]

        if parts in self.plan.explicit:
            raise RejectionError(
                RejectionCategory.DUPLICATE_ENTRY,
                f"entry {entry.name!r} duplicates an earlier entry",
                entry=entry.name,
            )

        folded = tuple(p.lower() for p in parts)
        earlier = self._fold_map.get(folded)
        if earlier is not None and earlier != parts:
            raise RejectionError(
                RejectionCategory.CASE_COLLISION,
                f"entry {entry.name!r} collides case-insensitively with "
                f"{'/'.join(earlier)!r}",
                entry=entry.name,
            )

        # A non-directory entry already occupying a prefix forbids children,
        # except a directory-style symlink which may legitimately carry
        # children (their physical paths are resolved later).
        for depth in range(1, len(parts)):
            prefix = parts[:depth]
            node = self.plan.nodes.get(prefix)
            if node is None:
                continue
            if node.kind is NodeKind.SYMLINK:
                continue
            if node.kind is not NodeKind.DIR:
                raise RejectionError(
                    RejectionCategory.PATH_CONFLICT,
                    f"entry {entry.name!r} sits under non-directory "
                    f"{node.relpath!r}",
                    entry=entry.name,
                )

        existing = self.plan.nodes.get(parts)
        if existing is not None and existing.implicit:
            if kind is not NodeKind.DIR:
                # A file/link claims a path that must be a directory because
                # children were declared earlier.
                raise RejectionError(
                    RejectionCategory.PATH_CONFLICT,
                    f"entry {entry.name!r} must be a directory: children were "
                    f"declared under it",
                    entry=entry.name,
                )
            # Explicit directory restating an implicit one: make it explicit.
            existing.implicit = False
            existing.raw = entry.name
            existing.mode = entry.mode
        else:
            self.plan.nodes[parts] = Node(
                parts=parts,
                raw=entry.name,
                kind=kind,
                index=entry.index,
                size=entry.size,
                mode=entry.mode,
                link_text=link_text,
            )

        self.plan.explicit.add(parts)
        if folded not in self._fold_map:
            self._fold_map[folded] = parts
        self.plan.order.append(parts)

        self._ensure_parent_dirs(parts)

        if kind is NodeKind.FILE:
            self.plan.usage.file_count += 1
            self.plan.usage.total_bytes += entry.size
        self.plan.usage.max_depth = max(self.plan.usage.max_depth, len(parts))

    def _ensure_parent_dirs(self, parts: tuple[str, ...]) -> None:
        for depth in range(1, len(parts)):
            prefix = parts[:depth]
            folded = tuple(p.lower() for p in prefix)
            earlier = self._fold_map.get(folded)
            if earlier is not None and earlier != prefix:
                raise RejectionError(
                    RejectionCategory.CASE_COLLISION,
                    f"directory prefix {'/'.join(prefix)!r} collides "
                    f"case-insensitively with {'/'.join(earlier)!r}",
                    entry="/".join(parts),
                )
            if prefix in self.plan.nodes:
                continue
            self.plan.nodes[prefix] = Node(
                parts=prefix,
                raw="/".join(prefix) + "/",
                kind=NodeKind.DIR,
                index=-1,
                implicit=True,
                mode=0o700,
            )
            self._fold_map[folded] = prefix

    # ------------------------------------------------------------------ #
    # Symlink graph
    # ------------------------------------------------------------------ #

    def resolve_symlinks(self) -> None:
        for node in self.plan.symlinks():
            assert node.link_text is not None
            if len(node.link_text.encode("utf-8", "surrogatepass")) > _MAX_LINK_TARGET_BYTES:
                raise RejectionError(
                    RejectionCategory.SYMLINK_ESCAPE,
                    f"symlink target exceeds {_MAX_LINK_TARGET_BYTES} bytes",
                    entry=node.relpath,
                )
            tokens = normalize_link_target(node.link_text, node.parts)
            resolved, target_node = self._resolve(
                tuple(tokens), set(), node.relpath
            )
            if target_node is None:
                raise RejectionError(
                    RejectionCategory.SYMLINK_DANGLING,
                    f"symlink {node.relpath!r} points to "
                    f"{'/'.join(resolved)!r}, which is not provided by the archive",
                    entry=node.relpath,
                )
            self.plan.link_targets[node.parts] = resolved

        # Regular files resolve through directory-chain symlinks to a physical
        # path that does not need to pre-exist in the graph (the file creates
        # it); two declared files landing on one physical path is a conflict.
        physical_owner: dict[tuple[str, ...], tuple[str, ...]] = {}
        for parts in list(self.plan.order):
            node = self.plan.nodes[parts]
            if node.kind is not NodeKind.FILE:
                continue
            resolved, _target = self._resolve(parts, set(), node.relpath)
            owner = physical_owner.get(resolved)
            if owner is not None and owner != parts:
                raise RejectionError(
                    RejectionCategory.PATH_CONFLICT,
                    f"entries {node.relpath!r} and {'/'.join(owner)!r} both "
                    f"materialize to {'/'.join(resolved)!r} through symlinks",
                    entry=node.relpath,
                )
            physical_owner[resolved] = parts
            self.plan.link_targets[parts] = resolved

    def _resolve(
        self,
        parts: tuple[str, ...],
        link_chain: set[tuple[str, ...]],
        entry_for_error: str,
    ) -> tuple[tuple[str, ...], Node | None]:
        """Walk ``parts`` virtually from the root, following directory symlinks."""
        cur: tuple[str, ...] = ()
        for position, component in enumerate(parts):
            cur = cur + (component,)
            node = self.plan.nodes.get(cur)
            if node is None:
                return cur, None
            if position < len(parts) - 1 and node.kind is NodeKind.FILE:
                # Descending *through* a regular file (not to it) is a type
                # conflict.  The final component is allowed to be a file.
                raise RejectionError(
                    RejectionCategory.PATH_CONFLICT,
                    f"path traverses non-directory node {node.relpath!r} while "
                    f"resolving {entry_for_error!r}",
                    entry=entry_for_error,
                )
            if node.kind is NodeKind.SYMLINK:
                if node.parts in link_chain:
                    raise RejectionError(
                        RejectionCategory.SYMLINK_LOOP,
                        f"symlink cycle detected at {node.relpath!r} while "
                        f"resolving {entry_for_error!r}",
                        entry=entry_for_error,
                    )
                if len(link_chain) >= self.budget.max_symlink_hops:
                    raise RejectionError(
                        RejectionCategory.SYMLINK_LOOP,
                        f"more than {self.budget.max_symlink_hops} symlink hops "
                        f"while resolving {entry_for_error!r}",
                        entry=entry_for_error,
                    )
                target_tokens = normalize_link_target(
                    node.link_text or "", node.parts
                )
                # Continue walking with the redirected components; targets not
                # represented in the graph are valid for file destinations.
                rest = parts[position + 1 :]
                cur, _ = self._resolve(
                    tuple(target_tokens) + rest,
                    link_chain | {node.parts},
                    entry_for_error,
                )
                return cur, self.plan.nodes.get(cur)
        return cur, self.plan.nodes.get(cur)

    def physical_parts(self, parts: tuple[str, ...]) -> tuple[str, ...]:
        """Resolve a node's declared path to its on-disk physical path parts."""
        resolved, node = self._resolve(parts, set(), "/".join(parts))
        if node is None:
            raise RejectionError(
                RejectionCategory.SYMLINK_DANGLING,
                f"path {'/'.join(parts)!r} is dangling",
                entry="/".join(parts),
            )
        return resolved
