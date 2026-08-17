"""Runtime configuration.

Everything here has a working default, so the app runs with no setup at all.
A Discogs personal-access token is optional: it lifts the catalog crawl from
25 to 60 requests/minute. Set it with DISCOGS_TOKEN if you have one.
"""
import os
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
ROOT_DIR = APP_DIR.parent
DATA_DIR = Path(os.environ.get("SAMPLETTE_DATA", ROOT_DIR / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "library.db"

HOST = os.environ.get("SAMPLETTE_HOST", "127.0.0.1")
PORT = int(os.environ.get("SAMPLETTE_PORT", "8733"))

# Identifies us to the public APIs. MusicBrainz requires a real UA or it 403s.
USER_AGENT = "samplette-local/1.0 (personal music discovery; +https://localhost)"

DISCOGS_TOKEN = os.environ.get("DISCOGS_TOKEN", "").strip()

# Requests per minute. Discogs publishes 25 unauthenticated / 60 authenticated;
# we stay just under. MusicBrainz asks for 1 req/sec.
DISCOGS_RPM = 55 if DISCOGS_TOKEN else 22
MUSICBRAINZ_RPM = 50
ACOUSTICBRAINZ_RPM = 60

# Background worker targets. The resolver keeps a buffer of playable tracks
# ready so pressing shuffle never waits on a YouTube search.
READY_BUFFER_TARGET = 400
CRAWL_BATCH_RELEASES = 25

# What the crawler digs through when you haven't told it otherwise. These are
# Discogs genre/style values, so they line up with the Style filter.
DEFAULT_SEED_GENRES = [
    "Electronic", "Rock", "Funk / Soul", "Jazz", "Hip Hop",
    "Latin", "Folk, World, & Country", "Reggae", "Blues", "Classical",
]
DEFAULT_SEED_STYLES = [
    "Minimal", "Synth-pop", "Post-Punk", "Disco", "Soul", "Psychedelic Rock",
    "Krautrock", "Ambient", "Dub", "Boogie", "Jazz-Funk", "New Wave",
    "Deep House", "Downtempo", "Afrobeat", "Bossa Nova", "Library Music",
    "Free Jazz", "Garage Rock", "Italo-Disco", "Cosmic", "Spiritual Jazz",
]
DEFAULT_SEED_DECADES = [1960, 1970, 1980, 1990, 2000]
