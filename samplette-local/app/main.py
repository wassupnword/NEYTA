"""FastAPI app: serves the UI and the JSON API it runs on.

Binds to 127.0.0.1 only — nothing here is exposed to the network.
"""
import csv
import io
import json
import random
import time
from typing import Any, Dict, List, Optional

from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import db, query
from .config import ROOT_DIR
from .crawler import crawler

app = FastAPI(title="Samplette Local", docs_url=None, redoc_url=None)

STATIC_DIR = ROOT_DIR / "static"
INDEX = STATIC_DIR / "index.html"
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.on_event("startup")
def _startup() -> None:
    db.init()
    if not db.q1("SELECT 1 FROM playlists LIMIT 1"):
        db.run("INSERT OR IGNORE INTO playlists (name, created_at) VALUES (?,?)",
               ("My playlist", time.time()))
    crawler.start()


@app.on_event("shutdown")
def _shutdown() -> None:
    crawler.stop()


@app.get("/")
def index() -> FileResponse:
    return FileResponse(str(INDEX))


# --------------------------------------------------------------- discovery

def _recent_ids(limit: int = 60) -> List[int]:
    rows = db.q("SELECT track_id FROM history ORDER BY played_at DESC LIMIT ?",
                (limit,))
    return [int(r["track_id"]) for r in rows]


@app.post("/api/next")
def next_track(payload: Dict[str, Any] = Body(default={})) -> Dict[str, Any]:
    """The shuffle button. Picks the next track for the active mode+filters."""
    mode = payload.get("mode") or "random"
    if mode not in query.MODES:
        raise HTTPException(400, "unknown mode")
    filters = payload.get("filters") or {}
    exclude_id = payload.get("exclude_id")
    playlist_id = payload.get("playlist_id")

    where, args = query.build_where(filters)
    from_clause = query.base_from(mode)

    if mode == "playlist":
        if not playlist_id:
            raise HTTPException(400, "playlist_id required")
        from_clause = ("tracks t JOIN playlist_tracks pt ON pt.track_id = t.id")
        where += " AND pt.playlist_id = ?"
        args = args + [int(playlist_id)]

    if mode == "for_you":
        track = _for_you_pick(where, args, exclude_id)
    else:
        sql = "SELECT t.* FROM {} WHERE {}".format(from_clause, where)

        # Avoid immediate repeats in shuffle, but only while the pool is big
        # enough to afford it. Counted before exclude_id is appended, so the
        # placeholders in `where` still match `args` exactly.
        if mode == "random":
            recent = _recent_ids(40)
            if recent:
                pool = db.q1(
                    "SELECT COUNT(*) AS n FROM {} WHERE {}".format(
                        from_clause, where), tuple(args))
                if pool and pool["n"] > len(recent) * 2:
                    sql += " AND t.id NOT IN ({})".format(
                        ",".join(str(i) for i in recent))

        if exclude_id:
            sql += " AND t.id != ?"
            args = args + [int(exclude_id)]
        sql += " ORDER BY {} LIMIT 1".format(query.order_for_mode(mode))
        row = db.q1(sql, tuple(args))
        track = db.track_dict(row) if row else None

    if not track:
        return {"track": None, "stats": db.stats(),
                "reason": "no tracks match the current filters yet"}
    return {"track": _decorate(track), "stats": db.stats()}


def _for_you_pick(where: str, args: list,
                  exclude_id: Optional[int]) -> Optional[Dict[str, Any]]:
    """Weighted pick from a candidate pool scored against your taste profile."""
    liked = db.q(
        """SELECT t.styles, t.genres, 3.0 AS w
             FROM favorites f JOIN tracks t ON t.id = f.track_id
           UNION ALL
           SELECT t.styles, t.genres, 1.0 AS w
             FROM (SELECT track_id FROM history GROUP BY track_id
                   HAVING COUNT(*) >= 2) hh
             JOIN tracks t ON t.id = hh.track_id""")
    profile = query.taste_profile(liked)

    sql = "SELECT t.* FROM tracks t WHERE {}".format(where)
    if exclude_id:
        sql += " AND t.id != ?"
        args = args + [int(exclude_id)]
    recent = _recent_ids(30)
    if recent:
        sql += " AND t.id NOT IN ({})".format(",".join(str(i) for i in recent))
    sql += " ORDER BY RANDOM() LIMIT 300"

    candidates = db.q(sql, tuple(args))
    if not candidates:
        return None
    if not profile:
        # Nothing learned yet, so For you is just shuffle until you rate things.
        return db.track_dict(random.choice(candidates))

    scored = [(query.score_against(profile, r), r) for r in candidates]
    scored.sort(key=lambda pair: pair[0], reverse=True)
    top = scored[: max(5, len(scored) // 10)]
    weights = [max(0.01, s) for s, _ in top]
    chosen = random.choices([r for _, r in top], weights=weights, k=1)[0]
    return db.track_dict(chosen)


def _decorate(track: Dict[str, Any]) -> Dict[str, Any]:
    """Attach the per-track user state the UI needs to render its controls."""
    tid = track["id"]
    track["is_favorite"] = bool(
        db.q1("SELECT 1 FROM favorites WHERE track_id=?", (tid,)))
    note = db.q1("SELECT body FROM notes WHERE track_id=?", (tid,))
    track["note"] = note["body"] if note else ""
    rows = db.q(
        "SELECT p.id, p.name FROM playlists p JOIN playlist_tracks pt "
        "ON pt.playlist_id = p.id WHERE pt.track_id=?", (tid,))
    track["in_playlists"] = [{"id": r["id"], "name": r["name"]} for r in rows]
    return track


@app.get("/api/track/{track_id}")
def get_track(track_id: int) -> Dict[str, Any]:
    row = db.q1("SELECT * FROM tracks WHERE id=?", (track_id,))
    if not row:
        raise HTTPException(404, "no such track")
    return {"track": _decorate(db.track_dict(row))}


@app.get("/api/related")
def related(track_id: int, kind: str = Query(...),
            limit: int = 40) -> Dict[str, Any]:
    """'More from this artist / release / channel / label' and 'more like this'."""
    row = db.q1("SELECT * FROM tracks WHERE id=?", (track_id,))
    if not row:
        raise HTTPException(404, "no such track")

    base = ("SELECT t.* FROM tracks t WHERE t.resolve_state='ready' "
            "AND t.id != ?")
    args: List[Any] = [track_id]

    if kind == "artist":
        base += " AND t.artist = ?"
        args.append(row["artist"])
    elif kind == "release":
        base += " AND t.release = ? AND t.release IS NOT NULL"
        args.append(row["release"])
    elif kind == "channel":
        base += " AND t.yt_channel = ? AND t.yt_channel IS NOT NULL"
        args.append(row["yt_channel"])
    elif kind == "label":
        base += " AND t.label = ? AND t.label IS NOT NULL"
        args.append(row["label"])
    elif kind == "similar":
        styles = db.track_dict(row)["styles"]
        if not styles:
            return {"tracks": []}
        base += " AND (" + " OR ".join("t.styles LIKE ?" for _ in styles) + ")"
        args.extend('%"{}"%'.format(s.replace('"', "")) for s in styles)
    else:
        raise HTTPException(400, "unknown kind")

    rows = db.q(base + " ORDER BY RANDOM() LIMIT ?", tuple(args + [limit]))
    return {"tracks": [db.track_dict(r) for r in rows]}


@app.post("/api/search")
def search(payload: Dict[str, Any] = Body(default={})) -> Dict[str, Any]:
    filters = payload.get("filters") or {}
    limit = int(payload.get("limit") or 60)
    where, args = query.build_where(filters)
    rows = db.q(
        "SELECT t.* FROM tracks t WHERE {} ORDER BY t.yt_views DESC LIMIT ?"
        .format(where), tuple(args + [limit]))
    return {"tracks": [db.track_dict(r) for r in rows]}


# ------------------------------------------------------------------ lists

@app.get("/api/list")
def list_mode(mode: str = "random", playlist_id: Optional[int] = None,
              limit: int = 50) -> Dict[str, Any]:
    """Contents of the playlist column for a given mode."""
    if mode == "favorites":
        rows = db.q(
            "SELECT t.* FROM tracks t JOIN favorites f ON f.track_id=t.id "
            "ORDER BY f.added_at DESC LIMIT ?", (limit,))
    elif mode == "recent_history":
        rows = db.q(
            "SELECT t.*, MAX(h.played_at) AS pa FROM tracks t "
            "JOIN history h ON h.track_id=t.id GROUP BY t.id "
            "ORDER BY pa DESC LIMIT ?", (limit,))
    elif mode == "playlist" and playlist_id:
        rows = db.q(
            "SELECT t.* FROM tracks t JOIN playlist_tracks pt "
            "ON pt.track_id=t.id WHERE pt.playlist_id=? "
            "ORDER BY pt.added_at DESC LIMIT ?", (playlist_id, limit))
    elif mode == "popular":
        rows = db.q(
            "SELECT t.* FROM tracks t WHERE t.resolve_state='ready' "
            "ORDER BY t.yt_views DESC LIMIT ?", (limit,))
    else:
        rows = db.q(
            "SELECT t.* FROM tracks t WHERE t.resolve_state='ready' "
            "ORDER BY RANDOM() LIMIT ?", (limit,))
    return {"tracks": [db.track_dict(r) for r in rows]}


@app.get("/api/filters/options")
def filter_options() -> Dict[str, Any]:
    """Distinct facet values present in the library, for the Filters panel."""
    def flatten(column: str) -> List[str]:
        seen: Dict[str, int] = {}
        for row in db.q(
                "SELECT {} AS v FROM tracks WHERE resolve_state='ready'"
                .format(column)):
            try:
                for value in json.loads(row["v"] or "[]"):
                    seen[value] = seen.get(value, 0) + 1
            except (ValueError, TypeError):
                continue
        return [k for k, _ in sorted(seen.items(), key=lambda kv: -kv[1])]

    regions = [r["region"] for r in db.q(
        "SELECT region, COUNT(*) c FROM tracks WHERE resolve_state='ready' "
        "AND region IS NOT NULL GROUP BY region ORDER BY c DESC")]
    keys = [r["musical_key"] for r in db.q(
        "SELECT musical_key, COUNT(*) c FROM tracks WHERE resolve_state='ready' "
        "AND musical_key IS NOT NULL GROUP BY musical_key ORDER BY c DESC")]
    bounds = db.q1(
        "SELECT MIN(year) miny, MAX(year) maxy, MIN(tempo) mint, "
        "MAX(tempo) maxt, MAX(yt_views) maxv FROM tracks "
        "WHERE resolve_state='ready'")
    return {
        "genres": flatten("genres"),
        "styles": flatten("styles"),
        "tags": flatten("tags"),
        "regions": regions,
        "keys": keys,
        "bounds": dict(bounds) if bounds else {},
    }


# ------------------------------------------------------- user state (local)

@app.post("/api/history")
def add_history(payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    tid = int(payload["track_id"])
    db.run("INSERT INTO history (track_id, played_at) VALUES (?,?)",
           (tid, time.time()))
    # Samplette keeps the last 1,000; so do we.
    db.run("DELETE FROM history WHERE id NOT IN "
           "(SELECT id FROM history ORDER BY played_at DESC LIMIT 1000)")
    return {"ok": True}


@app.post("/api/favorite")
def toggle_favorite(payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    tid = int(payload["track_id"])
    if db.q1("SELECT 1 FROM favorites WHERE track_id=?", (tid,)):
        db.run("DELETE FROM favorites WHERE track_id=?", (tid,))
        return {"is_favorite": False}
    db.run("INSERT INTO favorites (track_id, added_at) VALUES (?,?)",
           (tid, time.time()))
    return {"is_favorite": True}


@app.post("/api/note")
def save_note(payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    tid = int(payload["track_id"])
    body = (payload.get("body") or "").strip()
    if not body:
        db.run("DELETE FROM notes WHERE track_id=?", (tid,))
    else:
        db.run("INSERT INTO notes (track_id, body, updated_at) VALUES (?,?,?) "
               "ON CONFLICT(track_id) DO UPDATE SET body=excluded.body, "
               "updated_at=excluded.updated_at", (tid, body, time.time()))
    return {"ok": True, "note": body}


@app.get("/api/playlists")
def get_playlists() -> Dict[str, Any]:
    rows = db.q(
        "SELECT p.id, p.name, COUNT(pt.track_id) AS n FROM playlists p "
        "LEFT JOIN playlist_tracks pt ON pt.playlist_id = p.id "
        "GROUP BY p.id ORDER BY p.created_at")
    return {"playlists": [dict(r) for r in rows]}


@app.post("/api/playlists")
def create_playlist(payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    name = (payload.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "name required")
    if db.q1("SELECT 1 FROM playlists WHERE name=?", (name,)):
        raise HTTPException(409, "a playlist with that name already exists")
    cur = db.run("INSERT INTO playlists (name, created_at) VALUES (?,?)",
                 (name, time.time()))
    return {"id": cur.lastrowid, "name": name}


@app.delete("/api/playlists/{playlist_id}")
def delete_playlist(playlist_id: int) -> Dict[str, Any]:
    db.run("DELETE FROM playlists WHERE id=?", (playlist_id,))
    return {"ok": True}


@app.post("/api/playlists/{playlist_id}/tracks")
def playlist_add(playlist_id: int,
                 payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    tid = int(payload["track_id"])
    db.run("INSERT OR IGNORE INTO playlist_tracks "
           "(playlist_id, track_id, added_at) VALUES (?,?,?)",
           (playlist_id, tid, time.time()))
    return {"ok": True}


@app.delete("/api/playlists/{playlist_id}/tracks/{track_id}")
def playlist_remove(playlist_id: int, track_id: int) -> Dict[str, Any]:
    db.run("DELETE FROM playlist_tracks WHERE playlist_id=? AND track_id=?",
           (playlist_id, track_id))
    return {"ok": True}


@app.get("/api/playlists/{playlist_id}/export")
def export_playlist(playlist_id: int) -> StreamingResponse:
    row = db.q1("SELECT name FROM playlists WHERE id=?", (playlist_id,))
    if not row:
        raise HTTPException(404, "no such playlist")
    rows = db.q(
        "SELECT t.* FROM tracks t JOIN playlist_tracks pt ON pt.track_id=t.id "
        "WHERE pt.playlist_id=? ORDER BY pt.added_at", (playlist_id,))

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["Artist", "Title", "Release", "Year", "Label", "Region",
                     "Genre", "Style", "Key", "Tempo", "Views", "YouTube"])
    for r in rows:
        t = db.track_dict(r)
        writer.writerow([
            t["artist"], t["title"], t.get("release") or "", t.get("year") or "",
            t.get("label") or "", t.get("region") or "",
            ", ".join(t["genres"]), ", ".join(t["styles"]),
            t.get("musical_key") or "",
            "{:.0f}".format(t["tempo"]) if t.get("tempo") else "",
            t.get("yt_views") or "",
            "https://youtu.be/{}".format(t["yt_video_id"]),
        ])
    buf.seek(0)
    safe = "".join(c for c in row["name"] if c.isalnum() or c in " -_").strip()
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition":
                 'attachment; filename="{}.csv"'.format(safe or "playlist")},
    )


# ----------------------------------------------------------------- system

@app.get("/api/stats")
def get_stats() -> Dict[str, Any]:
    s = db.stats()
    s["crawler"] = dict(crawler.status)
    return s


@app.post("/api/seeds")
def set_seeds(payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    """Aim the crawler at particular genres/styles/years instead of the default mix."""
    seeds = payload.get("seeds")
    if seeds is None:
        db.run("DELETE FROM settings WHERE key='seed_config'")
        return {"ok": True, "seeds": None}
    if not isinstance(seeds, list):
        raise HTTPException(400, "seeds must be a list")
    db.set_setting("seed_config", json.dumps(seeds))
    return {"ok": True, "seeds": seeds}


@app.get("/api/seeds")
def get_seeds() -> Dict[str, Any]:
    raw = db.get_setting("seed_config")
    return {"seeds": json.loads(raw) if raw else None}


@app.exception_handler(Exception)
def _unhandled(_request, exc: Exception) -> JSONResponse:
    return JSONResponse(status_code=500,
                        content={"error": "{}: {}".format(type(exc).__name__, exc)})
