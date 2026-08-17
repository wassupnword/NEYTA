"""Key and tempo, via MusicBrainz -> AcousticBrainz.

Discogs has no MBIDs, so we look the recording up on MusicBrainz first, then
ask AcousticBrainz for its analysis.

Coverage is partial: AcousticBrainz stopped accepting submissions in 2022, so
obscure records often have no analysis. That is why the tap-tempo button (R)
exists on Samplette too — misses are expected, not a bug.
"""
from typing import Any, Dict, Optional

from ..config import ACOUSTICBRAINZ_RPM, MUSICBRAINZ_RPM
from . import RateLimiter, get_json

MB_API = "https://musicbrainz.org/ws/2"
AB_API = "https://acousticbrainz.org/api/v1"

_mb_limiter = RateLimiter(MUSICBRAINZ_RPM)
_ab_limiter = RateLimiter(ACOUSTICBRAINZ_RPM)

# Essentia reports the tonic and scale separately; Samplette shows "F minor".
_KEY_SUFFIX = {"major": "major", "minor": "minor"}


def _lucene_escape(text: str) -> str:
    for ch in ['\\', '+', '-', '&', '|', '!', '(', ')', '{', '}', '[', ']',
               '^', '"', '~', '*', '?', ':', '/']:
        text = text.replace(ch, "\\" + ch)
    return text


def find_recording_mbid(artist: str, title: str) -> Optional[str]:
    """Best-matching MusicBrainz recording id for an artist/title pair."""
    query = 'artist:"{}" AND recording:"{}"'.format(
        _lucene_escape(artist), _lucene_escape(title)
    )
    data = get_json(
        MB_API + "/recording",
        _mb_limiter,
        params={"query": query, "fmt": "json", "limit": 5},
    )
    if not data:
        return None
    for rec in data.get("recordings") or []:
        # MusicBrainz scores 0-100; below ~85 the match is usually a different
        # song that happens to share a word.
        if int(rec.get("score") or 0) >= 85 and rec.get("id"):
            return rec["id"]
    return None


def get_key_tempo(mbid: str) -> Optional[Dict[str, Any]]:
    """Key + BPM for a recording, or None when AcousticBrainz has no analysis."""
    data = get_json("{}/{}/low-level".format(AB_API.rsplit("/api/v1", 1)[0], mbid),
                    _ab_limiter)
    if not data:
        data = get_json(AB_API + "/low-level", _ab_limiter,
                        params={"recording_ids": mbid})
        if isinstance(data, dict) and mbid in data:
            data = (data.get(mbid) or {}).get("0")
    if not isinstance(data, dict):
        return None

    rhythm = data.get("rhythm") or {}
    tonal = data.get("tonal") or {}
    bpm = rhythm.get("bpm")
    key_root = tonal.get("key_key")
    key_scale = (tonal.get("key_scale") or "").lower()

    out: Dict[str, Any] = {}
    if isinstance(bpm, (int, float)) and 30 < float(bpm) < 300:
        out["tempo"] = round(float(bpm), 2)
    if key_root:
        scale = _KEY_SUFFIX.get(key_scale, key_scale)
        out["musical_key"] = "{} {}".format(key_root, scale).strip()
    return out or None


def enrich(artist: str, title: str) -> Optional[Dict[str, Any]]:
    """Full lookup chain. Returns {mb_recording_id, musical_key?, tempo?}."""
    mbid = find_recording_mbid(artist, title)
    if not mbid:
        return None
    out: Dict[str, Any] = {"mb_recording_id": mbid}
    analysis = get_key_tempo(mbid)
    if analysis:
        out.update(analysis)
    return out
