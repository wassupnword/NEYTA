"""The filter engine behind the Filters panel.

Mirrors Samplette's filter model: multi-select Genre / Style / Region / Key /
Tags each with "match all" and "exclude" toggles, numeric ranges for Tempo /
Views / Year, and a topic-channels-only switch.

Genres, styles and tags are JSON arrays in a text column. We match them with
LIKE on the quoted value, which is exact (values are quoted in the JSON) and
needs no JSON1 extension.
"""
import json
from typing import Any, Dict, List, Tuple

MODES = {"random", "for_you", "popular", "favorites", "recent_history", "playlist"}


def _as_list(value: Any) -> List[str]:
    if not value:
        return []
    if isinstance(value, str):
        return [value]
    return [str(v) for v in value if str(v).strip()]


def _json_contains(column: str, values: List[str], match_all: bool,
                   exclude: bool) -> Tuple[str, list]:
    """Build a predicate over a JSON-array text column."""
    terms = ['{} LIKE ?'.format(column) for _ in values]
    args = ['%"{}"%'.format(v.replace('"', '')) for v in values]
    joiner = " AND " if (match_all and not exclude) else " OR "
    clause = "(" + joiner.join(terms) + ")"
    if exclude:
        clause = "NOT " + clause
    return clause, args


def build_where(filters: Dict[str, Any]) -> Tuple[str, list]:
    """Return (sql_fragment, args) restricting to playable, matching tracks."""
    filters = filters or {}
    clauses = ["t.resolve_state = 'ready'", "t.yt_video_id IS NOT NULL"]
    args: List[Any] = []

    for field, column in (("genres", "t.genres"), ("styles", "t.styles"),
                          ("tags", "t.tags")):
        spec = filters.get(field) or {}
        values = _as_list(spec.get("values"))
        if not values:
            continue
        clause, a = _json_contains(column, values,
                                   bool(spec.get("match_all")),
                                   bool(spec.get("exclude")))
        clauses.append(clause)
        args.extend(a)

    for field, column in (("regions", "t.region"), ("keys", "t.musical_key")):
        spec = filters.get(field) or {}
        values = _as_list(spec.get("values"))
        if not values:
            continue
        placeholders = ",".join("?" for _ in values)
        if spec.get("exclude"):
            clauses.append(
                "({c} IS NULL OR {c} NOT IN ({p}))".format(c=column,
                                                           p=placeholders))
        else:
            clauses.append("{c} IN ({p})".format(c=column, p=placeholders))
        args.extend(values)

    for field, column in (("tempo", "t.tempo"), ("views", "t.yt_views"),
                          ("year", "t.year")):
        spec = filters.get(field) or {}
        lo, hi = spec.get("min"), spec.get("max")
        if lo not in (None, ""):
            clauses.append("{} >= ?".format(column))
            args.append(float(lo))
        if hi not in (None, ""):
            clauses.append("{} <= ?".format(column))
            args.append(float(hi))

    if filters.get("topic_only"):
        clauses.append("t.yt_is_topic = 1")

    text = (filters.get("q") or "").strip()
    if text:
        clauses.append("(t.artist LIKE ? OR t.title LIKE ? OR t.release LIKE ?)")
        like = "%{}%".format(text)
        args.extend([like, like, like])

    return " AND ".join(clauses), args


def order_for_mode(mode: str) -> str:
    if mode == "popular":
        return "t.yt_views DESC NULLS LAST"
    if mode == "recent_history":
        return "h.played_at DESC"
    if mode == "favorites":
        return "f.added_at DESC"
    return "RANDOM()"


def base_from(mode: str) -> str:
    """FROM clause; some modes join a user table."""
    if mode == "favorites":
        return "tracks t JOIN favorites f ON f.track_id = t.id"
    if mode == "recent_history":
        return ("tracks t JOIN (SELECT track_id, MAX(played_at) AS played_at "
                "FROM history GROUP BY track_id) h ON h.track_id = t.id")
    return "tracks t"


def taste_profile(rows: List[Any]) -> Dict[str, float]:
    """Weighted tag counts from tracks you've favorited or replayed.

    Stands in for the server-side "For you" model: it learns only from your own
    local listening, so it works with a single user.
    """
    weights: Dict[str, float] = {}
    for row in rows:
        weight = float(row["w"])
        for column in ("styles", "genres"):
            try:
                values = json.loads(row[column] or "[]")
            except (ValueError, TypeError):
                continue
            # Style is more discriminating than Genre, so it counts double.
            scale = weight * (2.0 if column == "styles" else 1.0)
            for value in values:
                weights[value] = weights.get(value, 0.0) + scale
    return weights


def score_against(profile: Dict[str, float], row: Any) -> float:
    if not profile:
        return 0.0
    score = 0.0
    for column in ("styles", "genres"):
        try:
            values = json.loads(row[column] or "[]")
        except (ValueError, TypeError):
            continue
        for value in values:
            score += profile.get(value, 0.0)
    return score
