"""Truth-preserving boolean simplification and canonical ordering.

Ordering note (requirement: simplification must preserve empty-query and
nonexistent-field semantics): field whitelist, type checks and the
complexity budget run in :mod:`searchdsl.validate` on the **parsed** tree
*before* this module runs. Normalization therefore only applies boolean
identities — it can never erase an unknown-field clause before that error
is reported, and :class:`MatchAll` (the empty query) is a fixed point.

Rewrite system (applied bottom-up, then repeated to a fixed point):

  flatten        AND/OR nested inside the same operator are merged.
  constants      AND(..., MatchNone, ...) -> MatchNone
                 OR(..., MatchAll, ...)   -> MatchAll
                 MatchAll is removed from AND; MatchNone from OR.
                 empty AND -> MatchAll ; empty OR -> MatchNone.
  idempotence    duplicate identical children collapse to one.
  complements    A and NOT A -> MatchNone ; A or NOT A -> MatchAll.
  double neg     NOT NOT A -> A ; NOT MatchAll -> MatchNone and back.
  absorption     A AND (A OR B) -> A ; A OR (A AND B) -> A.
  units          one-child AND/OR -> that child.
  ordering       remaining children are put in a total canonical order.

NOT is *not* distributed over AND/OR (no De Morgan expansion): doing so
can exponentially inflate the clause count, which conflicts with the
complexity budget. Uniqueness of the canonical form does not depend on it
because the same rewrite rules run to a fixed point and child order is
total.
"""

from __future__ import annotations

from searchdsl.astnodes import (
    And,
    MatchAll,
    MatchNone,
    Node,
    Not,
    Or,
    Phrase,
    canonical_json,
    first_pos,
    sort_nodes,
)

# Fixed-point iterations are bounded by tree size as a safety net.
_MAX_ROUNDS = 256


def _is_negation_of(a: Node, b: Node) -> bool:
    """True when *a* is NOT *b* (or vice versa), by canonical identity."""
    if isinstance(a, Not):
        return canonical_json(a.child) == canonical_json(b)
    if isinstance(b, Not):
        return canonical_json(b.child) == canonical_json(a)
    return False


def _norm_once(node: Node) -> Node:
    if isinstance(node, Not):
        inner = _norm_once(node.child)
        if isinstance(inner, Not):
            return inner.child.with_pos(first_pos(node, inner.child))
        if isinstance(inner, MatchAll):
            return MatchNone(pos=first_pos(node, inner))
        if isinstance(inner, MatchNone):
            return MatchAll(pos=first_pos(node, inner))
        return Not(inner, pos=first_pos(node, inner))

    if isinstance(node, (And, Or)):
        want = And if isinstance(node, And) else Or
        flat: list[Node] = []

        def collect(n: Node):
            if isinstance(n, want):
                for c in n.children:
                    collect(c)
            else:
                flat.append(_norm_once(n))

        for c in node.children:
            collect(c)

        # Constants.
        if want is And:
            for c in flat:
                if isinstance(c, MatchNone):
                    return MatchNone(pos=first_pos(node, c))
            flat = [c for c in flat if not isinstance(c, MatchAll)]
        else:
            for c in flat:
                if isinstance(c, MatchAll):
                    return MatchAll(pos=first_pos(node, c))
            flat = [c for c in flat if not isinstance(c, MatchNone)]

        # Deduplicate by canonical identity.
        seen: set[str] = set()
        deduped: list[Node] = []
        for c in flat:
            key = canonical_json(c)
            if key not in seen:
                seen.add(key)
                deduped.append(c)
        flat = deduped

        # Complements.
        for x in range(len(flat)):
            for y in range(x + 1, len(flat)):
                if _is_negation_of(flat[x], flat[y]):
                    if want is And:
                        return MatchNone(pos=first_pos(flat[x], flat[y]))
                    return MatchAll(pos=first_pos(flat[x], flat[y]))

        # Absorption: A AND (A OR B..) -> A ; A OR (A AND B..) -> A.
        absorbed: list[Node] = []
        for c in flat:
            if isinstance(c, (And, Or)) and isinstance(c, Or) == (want is And):
                inner_keys = {canonical_json(g) for g in c.children}
                if any(canonical_json(g) in inner_keys for g in flat if g is not c):
                    continue
            absorbed.append(c)
        flat = absorbed

        if not flat:
            return MatchAll(pos=node.pos) if want is And else MatchNone(pos=node.pos)
        if len(flat) == 1:
            return flat[0].with_pos(first_pos(node, flat[0]))
        ordered = sort_nodes(tuple(flat))
        return want(ordered, pos=first_pos(ordered[0], ordered[-1]))

    if isinstance(node, Phrase) and not node.value.strip():
        # An empty/whitespace-only phrase matches nothing.
        return MatchNone(pos=node.pos)

    return node


def normalize(node: Node) -> Node:
    """Rewrite *node* to its canonical form. Idempotent and total."""
    current = node
    previous = None
    rounds = 0
    while previous != canonical_json(current):
        previous = canonical_json(current)
        current = _norm_once(current)
        rounds += 1
        if rounds > _MAX_ROUNDS:  # pragma: no cover - defensive bound
            raise RuntimeError("normalization did not reach a fixed point")
    return current


def is_canonical(node: Node) -> bool:
    """True iff *node* equals its own normalization (idempotence check)."""
    return canonical_json(normalize(node)) == canonical_json(node)
