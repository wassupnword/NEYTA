"""Phrase search: find spoken words inside videos, and cut them out.

Build plan 5, and 2.2 before it. NEYTA has caption *extraction* for a video it
already knows, not a caption *index* of YouTube. Nothing in this project can
search all of YouTube for a phrase, and YouTube's own API only returns caption
tracks for videos the authenticated user owns.

So this is the search-then-verify hybrid: ask YouTube search for the phrase,
pull captions for the top N results, and match inside them. Reach is broad but
not exhaustive, and the UI says "searched the top 30 results for this phrase"
rather than "searched YouTube". If a Filmot key ever appears, step one is
replaced by a real index lookup and the wording changes accordingly.
"""

from __future__ import annotations

import concurrent.futures
import difflib
import logging
import random
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Sequence

import requests

from .. import config
from . import captions as caps
from .cache import Cache
from .engine import Engine, EngineError, RateLimited, backoff_delays

log = logging.getLogger(__name__)

ProgressFn = Callable[[float, str], None]

#: Seconds of padding either side of a matched span before cutting. Speech
#: runs into its neighbours, and silence-detect can tighten afterwards; it
#: cannot invent audio that was never fetched.
DEFAULT_PAD = 0.35


@dataclass(frozen=True)
class Hit:
    video_id: str
    title: str
    url: str
    uploader: str | None
    start_ms: int
    end_ms: int
    accuracy: caps.Accuracy
    context: str
    matched: str
    #: 1.0 for an exact token match, lower for a fuzzy one.
    score: float = 1.0

    @property
    def start(self) -> float:
        return self.start_ms / 1000.0

    @property
    def end(self) -> float:
        return self.end_ms / 1000.0

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)

    @property
    def tolerance_ms(self) -> int:
        return caps.TOLERANCE_MS[self.accuracy]

    @property
    def badge(self) -> str:
        return "word-accurate" if self.accuracy == "word" else "line-accurate"

    @property
    def needs_trimming(self) -> bool:
        """Line-accurate hits open with the trim handles active, because a
        two-second window is a starting point rather than an answer."""
        return self.accuracy == "line"

    def padded(self, pad: float = DEFAULT_PAD) -> tuple[float, float]:
        """The span to actually fetch. A line-accurate hit gets its whole
        caption line plus padding — cutting to a word timing we do not have
        would clip the phrase in half."""
        extra = pad + (self.tolerance_ms / 1000.0 if self.accuracy == "line" else 0.0)
        return max(0.0, self.start - extra), self.end + extra

    @property
    def label(self) -> str:
        minutes, seconds = divmod(int(self.start), 60)
        return f"{minutes}:{seconds:02d}"


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------


def find_in_track(
    track: caps.CaptionTrack,
    phrase: str,
    *,
    fuzzy: bool = True,
    threshold: float = 0.86,
    max_hits: int = 20,
) -> list[tuple[int, int, float]]:
    """Windows of the token stream matching `phrase`, as (first, last, score).

    Exact multi-token match first. The optional fuzzy pass catches the
    recogniser mishearing one word in a phrase, which is common enough that
    exact-only matching misses hits a human would accept.
    """
    needle = caps.tokenise(phrase)
    if not needle:
        return []
    tokens = track.tokens
    width = len(needle)
    if width > len(tokens):
        return []

    exact: list[tuple[int, int, float]] = []
    for i in range(len(tokens) - width + 1):
        if tokens[i:i + width] == needle:
            exact.append((i, i + width - 1, 1.0))
            if len(exact) >= max_hits:
                return exact
    if exact or not fuzzy:
        return exact

    # A single-token phrase has nothing to be approximately right about;
    # fuzzing it produces noise, not near-misses.
    if width == 1:
        return []

    target = " ".join(needle)
    fuzzy_hits: list[tuple[int, int, float]] = []
    matcher = difflib.SequenceMatcher(a=target, autojunk=False)
    for i in range(len(tokens) - width + 1):
        window = " ".join(tokens[i:i + width])
        matcher.set_seq2(window)
        # real_quick_ratio and quick_ratio are cheap upper bounds; skipping on
        # them keeps a 3,000-word track from costing thousands of full ratios.
        if matcher.real_quick_ratio() < threshold or matcher.quick_ratio() < threshold:
            continue
        score = matcher.ratio()
        if score >= threshold:
            fuzzy_hits.append((i, i + width - 1, score))

    fuzzy_hits.sort(key=lambda h: -h[2])
    return _drop_overlaps(fuzzy_hits)[:max_hits]


def _drop_overlaps(hits: list[tuple[int, int, float]]) -> list[tuple[int, int, float]]:
    """Keep the best of any overlapping fuzzy windows; a phrase sliding by one
    word scores nearly as well and would otherwise appear several times."""
    kept: list[tuple[int, int, float]] = []
    for hit in hits:
        if not any(hit[0] <= k[1] and k[0] <= hit[1] for k in kept):
            kept.append(hit)
    return kept


def hits_for(
    track: caps.CaptionTrack,
    phrase: str,
    *,
    title: str,
    url: str,
    uploader: str | None = None,
    fuzzy: bool = True,
    max_hits: int = 20,
) -> list[Hit]:
    out: list[Hit] = []
    for first, last, score in find_in_track(
        track, phrase, fuzzy=fuzzy, max_hits=max_hits
    ):
        words = track.words[first:last + 1]
        if not words:
            continue
        start_ms = words[0].start_ms
        if track.accuracy == "word":
            # Word timings mark starts, so the end of the last word is
            # unknown; carry it to the following word's start when there is
            # one, and fall back to a per-word estimate at the very end.
            following = track.words[last + 1] if last + 1 < len(track.words) else None
            end_ms = following.start_ms if following else start_ms + 600 * len(words)
        else:
            line = track.lines[words[-1].line_index]
            end_ms = line.end_ms
        out.append(Hit(
            video_id=track.video_id,
            title=title,
            url=url,
            uploader=uploader,
            start_ms=start_ms,
            end_ms=max(end_ms, start_ms + 200),
            accuracy=track.accuracy,
            context=track.context(first, last),
            matched=" ".join(w.text for w in words),
            score=score,
        ))
    return out


# ---------------------------------------------------------------------------
# Caption fetching
# ---------------------------------------------------------------------------


class CaptionFetcher:
    """Pulls json3 tracks, with the cache in front and backoff behind.

    HTTP 429 is real here — it was hit for real while testing the plan — so
    concurrency is bounded and every request can back off. The cache is what
    keeps ordinary use under the limit: caption data for a published video
    does not change, so a phrase searched twice, or a second phrase over a
    video already seen, costs nothing.
    """

    def __init__(
        self,
        engine: Engine,
        cache: Cache | None = None,
        session: Any | None = None,
        timeout: float = 30.0,
    ) -> None:
        self.engine = engine
        self.cache = cache if cache is not None else engine.cache
        self.session = session or requests.Session()
        self.timeout = timeout
        self._rng = random.Random()

    #: Marker for "this video has no usable captions". Cached like a real
    #: track, because otherwise every re-search pays a full extract for every
    #: candidate that will never yield a hit — which is most of them, and it
    #: was the difference between a 22-second re-run and an instant one.
    ABSENT = {"absent": True}

    def fetch(self, video_id: str, url: str) -> caps.CaptionTrack | None:
        """Captions for one video, from cache when possible."""
        if self.cache is not None:
            for kind in ("auto", "manual", "none"):
                cached = self.cache.get_captions(video_id, "en", kind)
                if cached == self.ABSENT:
                    return None
                if cached is not None:
                    return caps.CaptionTrack.from_json(cached)

        try:
            info = self.engine.extract(url, provider="youtube")
        except EngineError as exc:
            log.debug("no metadata for %s: %s", video_id, exc)
            return None

        chosen = caps.pick_track(info)
        if chosen is None:
            self._remember_absent(video_id)
            return None
        track_url, lang, kind = chosen

        payload = self._get_json(track_url)
        if payload is None:
            return None

        # Trust the data over the label: a track filed under automatic that
        # carries no word offsets is line-accurate whatever it claims.
        actual = caps.detect_kind(payload)
        track = caps.parse_json3(payload, video_id, lang, actual)
        if not track.words:
            self._remember_absent(video_id)
            return None

        if self.cache is not None:
            self.cache.put_captions(video_id, "en", track.to_json(), actual)
        return track

    def _remember_absent(self, video_id: str) -> None:
        if self.cache is not None:
            self.cache.put_captions(video_id, "en", self.ABSENT, "none")

    def _get_json(self, url: str) -> dict[str, Any] | None:
        delays = [0.0, *backoff_delays(rng=self._rng)]
        for attempt, delay in enumerate(delays):
            if delay:
                time.sleep(delay)
            try:
                response = self.session.get(url, timeout=self.timeout)
            except requests.RequestException as exc:
                log.debug("caption fetch failed: %s", exc)
                return None
            if response.status_code == 429:
                log.warning("429 fetching captions (attempt %s)", attempt + 1)
                continue
            if response.status_code != 200:
                return None
            try:
                return response.json()
            except ValueError:
                return None
        raise RateLimited("gave up fetching captions after repeated 429s")


# ---------------------------------------------------------------------------
# The pipeline
# ---------------------------------------------------------------------------


@dataclass
class PhraseSearch:
    phrase: str
    hits: list[Hit] = field(default_factory=list)
    #: Which engine produced this — see config.PHRASE_ENGINES. It changes what
    #: `searched` counts, and therefore what the summary is allowed to claim.
    engine: str = config.DEFAULT_PHRASE_ENGINE
    #: How many candidates were actually looked inside.
    searched: int = 0
    #: Candidates that had no usable captions at all.
    without_captions: int = 0
    #: Candidates abandoned because YouTube kept answering 429. Distinct from
    #: "no captions": those videos may well have them, and trying again in a
    #: minute is the right move rather than assuming they are barren.
    rate_limited: int = 0

    @property
    def word_accurate(self) -> list[Hit]:
        return [h for h in self.hits if h.accuracy == "word"]

    @property
    def summary(self) -> str:
        """The UI states its own limits rather than implying it searched all
        of YouTube.

        Each engine states its own: the built-in one read the top results, the
        index looked the phrase up. Saying "searched the top 30 results" about
        an index lookup would understate it; saying "searched YouTube" about
        either would be a lie.
        """
        found = len(self.hits)
        plural = "s" if found != 1 else ""
        if self.engine == "filmot":
            if not self.searched:
                return "Filmot's index has nothing for this phrase"
            return (
                f"searched Filmot's caption index — {found} hit{plural} "
                f"across {self.searched} video(s)"
            )
        if not self.searched:
            return "no candidates to search"
        base = (
            f"searched the top {self.searched} results for this phrase — "
            f"{found} hit{plural}"
        )
        if self.without_captions:
            base += f", {self.without_captions} had no captions"
        if self.rate_limited:
            base += (f", {self.rate_limited} rate-limited by YouTube "
                     "(try again shortly)")
        return base


def query_variants(phrase: str) -> list[str]:
    """The phrase, quoted, plus a small set of automatic variants.

    Quoting is what makes YouTube rank on the exact wording; the bare phrase
    catches videos whose title does not contain it but whose transcript does.
    """
    phrase = phrase.strip()
    if not phrase:
        return []
    variants = [f'"{phrase}"', phrase]
    words = phrase.split()
    if len(words) > 4:
        # A long phrase rarely appears verbatim in a title. Its opening is a
        # better search term than the whole thing.
        variants.append(" ".join(words[:4]))
    seen: list[str] = []
    for variant in variants:
        if variant not in seen:
            seen.append(variant)
    return seen


def discover(
    phrase: str,
    engine: Engine,
    *,
    candidates: int = config.PHRASE_CANDIDATES,
    fetcher: CaptionFetcher | None = None,
    workers: int = config.CAPTION_WORKERS,
    fuzzy: bool = True,
    progress: ProgressFn | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> PhraseSearch:
    """Search, fetch captions, match, rank."""
    search = PhraseSearch(phrase=phrase)
    if not phrase.strip():
        return search

    fetcher = fetcher or CaptionFetcher(engine)

    if progress:
        progress(0.05, "searching")

    entries: list[dict[str, Any]] = []
    seen: set[str] = set()
    for variant in query_variants(phrase):
        if should_cancel and should_cancel():
            return search
        try:
            found = engine.search("ytsearch", variant, candidates)
        except EngineError as exc:
            log.warning("phrase search failed for %r: %s", variant, exc)
            continue
        for entry in found:
            video_id = str(entry.get("id") or "")
            if video_id and video_id not in seen:
                seen.add(video_id)
                entries.append(entry)
        if len(entries) >= candidates:
            break
    entries = entries[:candidates]
    search.searched = len(entries)

    if not entries:
        return search

    def work(entry: dict[str, Any]) -> list[Hit]:
        if should_cancel and should_cancel():
            return []
        video_id = str(entry.get("id") or "")
        url = entry.get("webpage_url") or entry.get("url") or ""
        track = fetcher.fetch(video_id, url)
        if track is None:
            return []
        return hits_for(
            track, phrase,
            title=str(entry.get("title") or "untitled"),
            url=url,
            uploader=entry.get("uploader") or entry.get("channel"),
            fuzzy=fuzzy,
        )

    done = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(work, entry): entry for entry in entries}
        for future in concurrent.futures.as_completed(futures):
            done += 1
            if progress:
                progress(0.05 + 0.9 * done / len(entries),
                         f"reading captions {done}/{len(entries)}")
            try:
                found = future.result()
            except RateLimited:
                search.rate_limited += 1
                continue
            except Exception as exc:  # noqa: BLE001 — one bad video, not a bad search
                log.debug("candidate failed: %r", exc)
                continue
            if found:
                search.hits.extend(found)
            else:
                search.without_captions += 1

    # Word-accurate first, then by match quality, then by position in the
    # video — a hit near the start is easier to verify by ear.
    search.hits.sort(
        key=lambda h: (0 if h.accuracy == "word" else 1, -h.score, h.start_ms)
    )
    if progress:
        progress(1.0, search.summary)
    return search


def discover_with(
    phrase: str,
    engine: Engine,
    *,
    option: config.EngineOption,
    api_key: str | None = None,
    candidates: int = config.PHRASE_CANDIDATES,
    fuzzy: bool = True,
    progress: ProgressFn | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> PhraseSearch:
    """Run the phrase through whichever engine Settings chose.

    One entry point, so the panel above and the clip cutter below never learn
    which one answered — the same arrangement the tabs have with providers.
    `engine` is still the yt-dlp Engine, which the built-in path needs and the
    index path ignores.
    """
    if option.key == "filmot":
        # Imported here rather than at the top: filmot builds the Hit and
        # PhraseSearch defined in this module, so at module level the two
        # would import each other.
        from . import filmot as filmot_engine

        return filmot_engine.discover(
            phrase,
            filmot_engine.FilmotIndex(api_key=api_key or ""),
            candidates=candidates,
            progress=progress,
            should_cancel=should_cancel,
        )
    return discover(
        phrase, engine, candidates=candidates, fuzzy=fuzzy,
        progress=progress, should_cancel=should_cancel,
    )
