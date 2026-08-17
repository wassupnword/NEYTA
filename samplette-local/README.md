# Samplette Local

A local rebuild of [samplette.io](https://samplette.io) — the crate-digging tool
that shuffles you through obscure records with full metadata. Runs as a Python
program on your machine. No website to visit, no account, no PRO tier.

```bash
./run.sh
```

That's it. First run sets up a virtualenv, then opens
<http://localhost:8733> in your browser. The catalog builds itself in the
background while you listen.

---

## What it does

Press **Samplette** (or `D`) and you get a record you've probably never heard,
with the metadata panel filled in: artist, release, year, channel, views, key,
tempo, genre, style, region, label, and both copyright lines. Filter it down to
*1970s Brazilian jazz-funk in F minor between 90 and 100 BPM* and keep digging
inside that.

Everything the website put behind an account or the PRO tier is just on:
unlimited filtering, playlists, favorites, history, notes, CSV export.

### Keyboard

The website's shortcuts, unchanged:

| Key | Action | Key | Action |
|-----|--------|-----|--------|
| `D` | Shuffle a new track | `S` | Filters |
| `Q` | For you | `A` | Jump to metadata |
| `W` | Popular | `R` | Tap for tempo |
| `E` | Favorite | `C` | Copy YouTube link |

---

## How it works

Samplette's value is a server-side database of YouTube music joined to
record-shop metadata. That database isn't downloadable — the site only ever
sends your browser one track at a time. So this builds its own, from the same
public sources the site credits on its metadata panel:

```
Discogs search      ──▶  release IDs
Discogs release     ──▶  tracklist + genre/style/label/region/year/©/℗
YouTube search      ──▶  the video, channel, view count
MusicBrainz         ──▶  recording ID
AcousticBrainz      ──▶  key + tempo
```

Four background threads run that pipeline continuously into a SQLite file at
`data/library.db`. Only the YouTube stage gates playback, so tracks become
playable within about a minute of starting, and the library keeps growing the
whole time you're using it — a few thousand tracks in an evening.

Playback is YouTube's official IFrame embed, exactly as the website does it.
**Nothing is downloaded**; `yt-dlp` is used only to search, in flat mode, which
never touches the video player.

### No API keys

All four sources work unauthenticated. A free
[Discogs token](https://www.discogs.com/settings/developers) is optional and
only raises the crawl rate from 25 to 60 requests/minute:

```bash
DISCOGS_TOKEN=your_token ./run.sh
```

---

## Aiming it

By default the crawler roams across a spread of genres, styles and decades.
To dig somewhere specific, open **⚙ → What to dig for** and give it Discogs
styles:

```
Minimal, Post-Punk, Spiritual Jazz, Library Music
```

Optionally bound it by year. New crawling follows the new instruction;
what's already in the library stays.

---

## Honest differences from the website

Three of Samplette's features are inherently server-side and can't be
reproduced by a single-user local program. Two are replaced, one is dropped:

| Website | Here |
|---------|------|
| **Trending** — what everyone is playing right now | **Popular** — highest YouTube view count in *your* library. Different thing, honestly labelled. |
| **For you** — trained on all users | **For you** — scored against your own favorites and repeat plays. Works, but needs you to favorite a few things first. |
| **Comments** — other people's | **Dropped.** Private notes (`✎ Notes`) cover the same ground for one person. |

Two more things to expect:

- **Key and tempo are often blank.** AcousticBrainz stopped accepting
  submissions in 2022, so obscure records frequently have no analysis. The
  website has the same gap — it's why the tap-tempo button exists there too.
  Tap `R` in time and it counts BPM for you.
- **Your library is what you've crawled.** The site has years of accumulated
  catalog; yours starts empty and grows. Filters only ever show values actually
  present in your library, so the panel fills out as you dig.

---

## Layout

```
app/
  config.py            settings, rate limits, default seed genres/styles
  db.py                SQLite schema + helpers
  query.py             the filter engine
  crawler.py           the four background workers
  main.py              FastAPI routes
  sources/
    discogs.py         catalog + genre/style/label/region/©/℗
    acousticbrainz.py  key + tempo, via MusicBrainz lookup
    youtube.py         video search + match scoring
static/                the interface (no build step, no CDN)
data/library.db        your catalog, playlists, favorites, history, notes
```

Your data is one SQLite file. Back it up by copying `data/library.db`; delete
it to start over.

## Notes

- Binds to `127.0.0.1` only — nothing is exposed to your network.
- Change the port with `./run.sh --port 9000`.
- Stop with `Ctrl+C`.
- Your Mac's system Python is 3.9, which works but which `yt-dlp` now prints a
  deprecation notice for on every search. Harmless today; `yt-dlp` will drop 3.9
  eventually. When it does, `brew install python@3.12`, delete `.venv`, and
  re-run `./run.sh` to rebuild the environment. Your library isn't affected —
  it lives in `data/library.db`.
