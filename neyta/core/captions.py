"""json3 captions -> a token stream with timings.

Build plan 2.3, confirmed live before writing this: the two kinds of caption
track are not interchangeable.

  auto-generated   one seg per word, each carrying `tOffsetMs` relative to its
                   event's `tStartMs`. Measured on a TED talk: 646 events,
                   2575 segs, 1930 of them with word offsets.
  human-uploaded   one seg per line, `tOffsetMs` absent entirely. Measured on
                   "Me at the zoo": 260 events, 260 segs, zero offsets.

So a phrase hit is either word-accurate (±50ms) or line-accurate (±2s), and
the two must never be presented as the same thing. Everything downstream —
the badge in the hit list, whether the trim handles open active — follows from
`CaptionTrack.accuracy`.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Iterable, Literal

Accuracy = Literal["word", "line"]
Kind = Literal["auto", "manual"]

#: How far a hit of each quality may be from the true utterance.
TOLERANCE_MS: dict[Accuracy, int] = {"word": 50, "line": 2000}

_KEEP = re.compile(r"[^a-z0-9]+")


def normalise(text: str) -> str:
    """Fold text to a bare comparison form.

    Auto-captions vary in punctuation and case between videos and between
    releases of YouTube's recogniser, so both sides of a comparison are
    stripped to letters and digits. "Don't" and "dont" have to match, because
    which one you get is not something the user can predict.
    """
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    return _KEEP.sub("", text.lower())


def tokenise(text: str) -> list[str]:
    return [t for t in (normalise(part) for part in text.split()) if t]


@dataclass(frozen=True)
class Word:
    token: str
    text: str
    start_ms: int
    #: The caption line this came from, so a hit can quote its context.
    line_index: int

    @property
    def start(self) -> float:
        return self.start_ms / 1000.0


@dataclass(frozen=True)
class Line:
    text: str
    start_ms: int
    duration_ms: int

    @property
    def end_ms(self) -> int:
        return self.start_ms + self.duration_ms


@dataclass(frozen=True)
class CaptionTrack:
    video_id: str
    lang: str
    kind: Kind
    words: tuple[Word, ...]
    lines: tuple[Line, ...]

    @property
    def accuracy(self) -> Accuracy:
        """Auto-captions land on the syllable; manual ones land on the line."""
        return "word" if self.kind == "auto" else "line"

    @property
    def tolerance_ms(self) -> int:
        return TOLERANCE_MS[self.accuracy]

    @property
    def tokens(self) -> list[str]:
        return [w.token for w in self.words]

    def context(self, first: int, last: int, span: int = 8) -> str:
        """The matched words with a few on either side, as written."""
        lo = max(0, first - span)
        hi = min(len(self.words), last + span + 1)
        return " ".join(w.text for w in self.words[lo:hi]).strip()

    def to_json(self) -> dict[str, Any]:
        """A cacheable form. Caption data for a published video does not
        change, so this is stored without expiry."""
        return {
            "video_id": self.video_id,
            "lang": self.lang,
            "kind": self.kind,
            "words": [[w.token, w.text, w.start_ms, w.line_index] for w in self.words],
            "lines": [[ln.text, ln.start_ms, ln.duration_ms] for ln in self.lines],
        }

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> "CaptionTrack":
        return cls(
            video_id=payload["video_id"],
            lang=payload["lang"],
            kind=payload["kind"],
            words=tuple(Word(t, x, s, i) for t, x, s, i in payload["words"]),
            lines=tuple(Line(t, s, d) for t, s, d in payload["lines"]),
        )


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def _events(payload: dict[str, Any]) -> Iterable[dict[str, Any]]:
    for event in payload.get("events") or []:
        segs = event.get("segs")
        if not segs:
            # The first event of an auto-caption track is a window definition
            # with no text at all.
            continue
        if all(not (s.get("utf8") or "").strip() for s in segs):
            # A rollup event: `aAppend: 1` with a lone newline, present only to
            # scroll the on-screen window. It carries no words.
            continue
        yield event


def parse_json3(payload: dict[str, Any], video_id: str, lang: str,
                kind: Kind) -> CaptionTrack:
    """Build a word stream from one json3 track.

    For an auto track each seg is a word and carries its own offset. For a
    manual track the seg is a whole line, so every token in it gets the line's
    start time — which is precisely why those hits are badged line-accurate
    rather than pretending to a precision the data does not have.
    """
    words: list[Word] = []
    lines: list[Line] = []

    for event in _events(payload):
        start = int(event.get("tStartMs") or 0)
        duration = int(event.get("dDurationMs") or 0)
        segs = event.get("segs") or []
        line_index = len(lines)

        text = "".join(s.get("utf8") or "" for s in segs)
        lines.append(Line(text=text.replace("\n", " ").strip(),
                          start_ms=start, duration_ms=duration))

        has_offsets = any("tOffsetMs" in s for s in segs)
        if has_offsets:
            for seg in segs:
                raw = (seg.get("utf8") or "").strip()
                if not raw:
                    continue
                # The first seg of an event has no tOffsetMs; it starts with
                # the event. Absent means zero, not missing.
                at = start + int(seg.get("tOffsetMs") or 0)
                for part in raw.split():
                    token = normalise(part)
                    if token:
                        words.append(Word(token, part, at, line_index))
        else:
            for part in text.split():
                token = normalise(part)
                if token:
                    words.append(Word(token, part, start, line_index))

    return CaptionTrack(
        video_id=video_id, lang=lang, kind=kind,
        words=tuple(words), lines=tuple(lines),
    )


def detect_kind(payload: dict[str, Any]) -> Kind:
    """Decide from the data rather than from where it was fetched.

    A track advertised as automatic that carries no word offsets is a manual
    track for every purpose that matters here.
    """
    for event in payload.get("events") or []:
        for seg in event.get("segs") or []:
            if "tOffsetMs" in seg:
                return "auto"
    return "manual"


# ---------------------------------------------------------------------------
# Track selection
# ---------------------------------------------------------------------------


def pick_track(
    info: dict[str, Any], languages: tuple[str, ...] = ("en", "en-US", "en-GB")
) -> tuple[str, str, Kind] | None:
    """(url, lang, kind) for the best caption track on a video, or None.

    Auto-captions are preferred over manual ones — the opposite of what you
    would want for reading, and exactly right here, because word timing is the
    whole point and only the automatic track has it.
    """
    automatic = info.get("automatic_captions") or {}
    manual = info.get("subtitles") or {}

    for source, kind in ((automatic, "auto"), (manual, "manual")):
        for lang in languages:
            for track in source.get(lang) or []:
                if track.get("ext") == "json3" and track.get("url"):
                    return track["url"], lang, kind  # type: ignore[return-value]

    # Any English-ish variant, then anything at all: a hit in the wrong dialect
    # is still a hit.
    for source, kind in ((automatic, "auto"), (manual, "manual")):
        for lang, tracks in source.items():
            if not lang.startswith("en"):
                continue
            for track in tracks or []:
                if track.get("ext") == "json3" and track.get("url"):
                    return track["url"], lang, kind  # type: ignore[return-value]
    return None


def available_languages(info: dict[str, Any]) -> dict[str, list[str]]:
    return {
        "automatic": sorted(info.get("automatic_captions") or {}),
        "manual": sorted(info.get("subtitles") or {}),
    }
