"""Phrase search against Filmot's caption index.

The other half of the choice offered in Settings. The built-in engine in
phrase.py asks YouTube for the phrase and then reads the captions of the top
results — broad, honest about its reach, and free. This one looks the phrase up
in an index of what has actually been said, so it answers "which video says
this" directly instead of inferring it from a ranking.

What that buys, in the words the UI uses: the built-in engine searched the top
30 results, and this one searched the index. Neither claims to have searched
YouTube.

Two things follow from Filmot returning a timestamp rather than a transcript:

  * hits are line-accurate, never word-accurate. The index stores where a
    caption line begins; it does not hand back per-word offsets, so a hit opens
    with the trim handles active exactly like a manual-caption hit from the
    built-in engine.
  * there is no end time. The end is estimated from the length of the phrase
    and widened by the line tolerance before anything is cut, which is the
    same treatment `Hit.padded` already gives a line-accurate hit.

The response shape is read defensively. Filmot publishes no schema — the field
names here come from the community wrapper at github.com/dusking/filmot — so
each field is looked for under the spellings that have been seen rather than
one assumed name, and an item that carries none of them is skipped instead of
crashing the search.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Sequence

import requests

from . import captions as caps
from .phrase import Hit, PhraseSearch

log = logging.getLogger(__name__)

ProgressFn = Callable[[float, str], None]

#: Filmot's API is served through RapidAPI; the supporter key is a RapidAPI
#: key for this host.
DEFAULT_HOST = "filmot-tube-metadata-archive.p.rapidapi.com"
SEARCH_PATH = "getsearchsubtitles"

#: Milliseconds of speech to assume per word when estimating where a hit ends.
#: Filmot gives a start and nothing else; this is only ever a first guess that
#: the pad and the trim handles then correct.
MS_PER_WORD = 600


class FilmotError(RuntimeError):
    pass


class FilmotUnavailable(FilmotError):
    """No key, or the key was refused."""


def _first(item: dict[str, Any], *names: str) -> Any:
    for name in names:
        value = item.get(name)
        if value not in (None, ""):
            return value
    return None


def _video_id(item: dict[str, Any]) -> str | None:
    value = _first(item, "id", "videoid", "video_id", "vid")
    return str(value) if value else None


def _seconds(value: Any) -> float | None:
    """Filmot returns seconds, sometimes as a string. Milliseconds are not a
    shape it has been seen to use, so a bare number is trusted as seconds."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


@dataclass
class FilmotIndex:
    """The client. One call per search; nothing here is cached.

    Caption *data* is what the local cache is for, and this engine never
    fetches any — it gets an answer, not a transcript.
    """

    api_key: str
    host: str = DEFAULT_HOST
    session: Any | None = None
    timeout: float = 30.0
    lang: str = "en"

    def __post_init__(self) -> None:
        if not self.api_key:
            raise FilmotUnavailable(
                "no Filmot key — paste one in Settings, or switch the phrase "
                "engine back to NEYTA"
            )
        if self.session is None:
            self.session = requests.Session()

    @property
    def url(self) -> str:
        return f"https://{self.host}/{SEARCH_PATH}"

    def _headers(self) -> dict[str, str]:
        return {
            "X-RapidAPI-Key": self.api_key,
            "X-RapidAPI-Host": self.host,
        }

    def search(self, phrase: str, *, limit: int = 30) -> list[dict[str, Any]]:
        """Raw index rows for a phrase.

        The phrase is sent quoted, which is how Filmot's own syntax asks for
        an exact match rather than a bag of words.
        """
        phrase = phrase.strip()
        if not phrase:
            return []
        params = {
            "query": f'"{phrase}"',
            "lang": self.lang,
            "limit": str(limit),
        }
        try:
            response = self.session.get(
                self.url, params=params, headers=self._headers(),
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise FilmotError(f"Filmot did not answer: {exc}") from exc

        if response.status_code in (401, 403):
            raise FilmotUnavailable(
                "Filmot refused the key — check it in Settings"
            )
        if response.status_code == 429:
            raise FilmotError("Filmot rate-limited this key; try again shortly")
        if response.status_code != 200:
            raise FilmotError(f"Filmot answered {response.status_code}")

        try:
            payload = response.json()
        except ValueError as exc:
            raise FilmotError("Filmot returned something that is not JSON") from exc

        rows = payload.get("result") if isinstance(payload, dict) else payload
        if not isinstance(rows, list):
            return []
        return [row for row in rows if isinstance(row, dict)]


def hits_from_row(row: dict[str, Any], phrase: str) -> list[Hit]:
    """The hits inside one index row.

    A row is a video plus every place in it the phrase occurs, so one row can
    be several hits — which is the whole point of an index over a ranking.
    """
    video_id = _video_id(row)
    if not video_id:
        return []
    title = str(_first(row, "title", "name") or "untitled")
    uploader = _first(row, "channelname", "channel_name", "channel", "uploader")
    raw_hits = _first(row, "hits", "subtitles", "matches") or []
    if not isinstance(raw_hits, Iterable) or isinstance(raw_hits, (str, bytes)):
        return []

    words = max(len(caps.tokenise(phrase)), 1)
    out: list[Hit] = []
    for raw in raw_hits:
        if not isinstance(raw, dict):
            continue
        start = _seconds(_first(raw, "start", "start_time", "time", "offset"))
        if start is None:
            continue
        start_ms = int(start * 1000)
        before = str(_first(raw, "ctx_before", "before", "context_before") or "")
        after = str(_first(raw, "ctx_after", "after", "context_after") or "")
        out.append(Hit(
            video_id=video_id,
            title=title,
            # The timestamp goes in the URL as well as in the hit: the row is
            # a link you can open outside the app and land in the right place.
            url=f"https://www.youtube.com/watch?v={video_id}&t={int(start)}s",
            uploader=str(uploader) if uploader else None,
            start_ms=start_ms,
            end_ms=start_ms + MS_PER_WORD * words,
            # The index knows the line, not the word. Saying "word" here would
            # put a word-accurate badge on a two-second window.
            accuracy="line",
            context=" ".join(part for part in (before, phrase, after) if part),
            matched=phrase,
            score=1.0,
        ))
    return out


def discover(
    phrase: str,
    index: FilmotIndex,
    *,
    candidates: int = 30,
    progress: ProgressFn | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> PhraseSearch:
    """The same shape `phrase.discover` returns, from the index instead.

    Deliberately the same return type: the panel, the player and the clip
    cutter downstream do not learn which engine found the hit, exactly as the
    tabs do not learn which service found the track.
    """
    search = PhraseSearch(phrase=phrase)
    if not phrase.strip():
        return search
    if should_cancel and should_cancel():
        return search

    if progress:
        progress(0.1, "asking Filmot")

    rows = index.search(phrase, limit=candidates)
    search.searched = len(rows)
    search.engine = "filmot"

    for row in rows:
        if should_cancel and should_cancel():
            return search
        found = hits_from_row(row, phrase)
        if found:
            search.hits.extend(found)
        else:
            # A row with no readable timestamp is a row we cannot take you to.
            search.without_captions += 1

    # No word-accurate tier to sort into, so position in the video is the only
    # ordering left that means anything: an early hit is quicker to verify.
    search.hits.sort(key=lambda h: (h.title.lower(), h.start_ms))
    if progress:
        progress(1.0, search.summary)
    return search


def summarise(rows: Sequence[dict[str, Any]]) -> str:
    """Used by the settings page to prove a key works without running a search
    the user did not ask for."""
    return f"{len(rows)} video(s) matched"
