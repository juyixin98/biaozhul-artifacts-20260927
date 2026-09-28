"""Query orchestration: normalize, retrieve, score, rank, explain.

Failure categories live in :mod:`app.errors`. Situations that are not hard
failures but make an answer *incomplete* are returned under ``uncertainties``:
length-window truncation (an unscored candidate could exist), scoring-cap
truncation (a passed-bound candidate was not scored) and result truncation.

Ranking is fully deterministic. For each candidate the sort key is

    (distance asc, term asc, frequency desc)

so distance dominates, and ties break lexicographically first and on usage
frequency second — never on SQLite scan order or Python set iteration.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from . import core, indexing
from .config import EPS, Settings
from .errors import QueryTooLongError, VersionNotFoundError
from .normalization import Token, normalize_and_tokenize
from .storage import VersionStore


@dataclass
class Correction:
    token: str
    candidate: str
    distance: float
    frequency: float
    path: list[dict]
    path_recomputed_cost: float
    path_matches: bool


@dataclass
class TokenReport:
    token: Token
    exact_match: bool
    corrections: list[Correction]
    stage: dict = field(default_factory=dict)


@dataclass
class QueryReport:
    request_id: str
    version_id: int
    version_active: bool
    normalized_query: str
    threshold: float
    token_reports: list[TokenReport]
    uncertainties: list[str]
    corrected_text: str


class SpellcheckService:
    def __init__(self, settings: Settings, store: VersionStore | None = None):
        self.settings = settings
        self.store = store or VersionStore(settings.db_path)

    # ------------------------------------------------------------------ #
    def correct(
        self,
        query: str,
        *,
        request_id: str,
        threshold: float | None = None,
        max_results: int | None = None,
        version_id: int | None = None,
        include_paths: bool = True,
    ) -> QueryReport:
        limits = self.settings.limits
        if query is not None and len(query) > limits.max_query_chars:
            raise QueryTooLongError(
                f"query exceeds {limits.max_query_chars} characters",
                details={"limit": limits.max_query_chars, "actual": len(query)},
            )
        if threshold is None:
            threshold = limits.default_threshold
        if threshold < 0:
            threshold = 0.0
        if max_results is None:
            max_results = limits.max_results_per_token
        max_results = max(1, min(int(max_results), limits.max_results_per_token))

        try:
            resolved = self.store.resolve_version(version_id)
        except KeyError:
            raise VersionNotFoundError(
                f"dictionary version {version_id} does not exist"
                if version_id is not None
                else "no active dictionary version"
            )

        normalized, tokens = normalize_and_tokenize(
            query,
            alphabet=self.settings.alphabet_set,
            max_tokens=limits.max_query_tokens,
        )

        uncertainties: list[str] = []
        reports: list[TokenReport] = []
        for tok in tokens:
            reports.append(
                self._correct_token(
                    tok,
                    version_id=resolved,
                    threshold=float(threshold),
                    max_results=max_results,
                    include_paths=include_paths,
                    uncertainties=uncertainties,
                )
            )

        active = self.store.active_version()
        text = self._render_corrected_text(normalized, reports)
        return QueryReport(
            request_id=request_id,
            version_id=resolved,
            version_active=(active == resolved),
            normalized_query=normalized,
            threshold=float(threshold),
            token_reports=reports,
            uncertainties=uncertainties,
            corrected_text=text,
        )

    # ------------------------------------------------------------------ #
    def _correct_token(
        self,
        token: Token,
        *,
        version_id: int,
        threshold: float,
        max_results: int,
        include_paths: bool,
        uncertainties: list[str],
    ) -> TokenReport:
        costs = self.settings.costs
        text = token.text

        candidates, pstats = indexing.retrieve_candidates(
            self.store,
            version_id,
            text,
            threshold,
            costs,
            sql_limit=self.settings.limits.max_candidates_scored,
        )

        if pstats.window_truncated:
            uncertainties.append(
                f"token {text!r}: length window contained "
                f"{pstats.length_window_count} rows but only "
                f"{pstats.retrieved} were retrieved "
                f"(limit {self.settings.limits.max_candidates_scored}); "
                "qualifying candidates may be unscored"
            )

        scored: list[tuple[float, str, float, list[core.Operation]]] = []
        exact = False
        cap_hits: list[str] = []
        for row in candidates:
            term = row["term"]
            try:
                result = core.distance_with_path(
                    text, term, costs,
                    threshold=threshold,
                    node_cap=self.settings.limits.max_search_nodes,
                )
            except core.SearchCapExceeded:
                cap_hits.append(term)
                continue
            if result.distance <= threshold + EPS:
                scored.append((result.distance, term, float(row["frequency"]),
                               result.operations))
            if term == text and result.distance <= EPS:
                exact = True

        if cap_hits:
            uncertainties.append(
                f"token {text!r}: A* node cap "
                f"({self.settings.limits.max_search_nodes}) hit for "
                f"{len(cap_hits)} candidate(s) (e.g. {cap_hits[0]!r}); "
                "their distance was not decided"
            )

        # Deterministic rank: distance, then term, then frequency desc.
        scored.sort(key=lambda x: (round(x[0], 9), x[1], -x[2]))

        top = scored[:max_results]
        if len(scored) > max_results:
            uncertainties.append(
                f"token {text!r}: {len(scored)} candidates within threshold "
                f"{threshold}; only top {max_results} returned"
            )

        corrections: list[Correction] = []
        for dist, term, freq, ops in top:
            replayed = core.replay(text, ops)
            recost = core.recompute_cost(ops, costs)
            corrections.append(
                Correction(
                    token=text,
                    candidate=term,
                    distance=dist,
                    frequency=freq,
                    path=[o.to_dict() for o in ops] if include_paths else [],
                    path_recomputed_cost=recost,
                    path_matches=(replayed == term and abs(recost - dist) <= EPS),
                )
            )

        return TokenReport(
            token=token,
            exact_match=exact,
            corrections=corrections,
            stage={
                "length_window": [pstats.min_len, pstats.max_len],
                "length_window_rows": pstats.length_window_count,
                "rows_retrieved": pstats.retrieved,
                "rejected_by_lower_bounds": pstats.bounds_rejected,
                "passed_lower_bounds": pstats.passed_bounds,
                "within_threshold": len(scored),
            },
        )

    @staticmethod
    def _render_corrected_text(normalized: str, reports: list[TokenReport]) -> str:
        pieces: list[str] = []
        cursor = 0
        for report in reports:
            tok = report.token
            pieces.append(normalized[cursor:tok.start])
            # Preserve the token when it is already correct; otherwise take
            # the best candidate only when one exists.
            if report.exact_match or not report.corrections:
                pieces.append(tok.text)
            else:
                pieces.append(report.corrections[0].candidate)
            cursor = tok.end
        pieces.append(normalized[cursor:])
        return "".join(pieces)
