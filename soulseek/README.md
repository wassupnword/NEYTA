# soulseek_api

A small, reusable Python client for Soulseek. Drop the `soulseek_api/` folder into
any future project and you have search + download in a few lines.

```python
from soulseek_api import SoulseekClient

with SoulseekClient.from_env() as sk:
    files = sk.search("aphex twin xtal", timeout=20, audio_only=True)
    best = sk.best_match(files, prefer_extensions=["flac", "mp3"])
    sk.download_and_wait(best)
```

## How this works

Soulseek has no official HTTP API — it's a proprietary TCP protocol. The standard
way to script it is **[slskd](https://github.com/slskd/slskd)**, a headless Soulseek
client that logs into the network for you and exposes a REST API. This package is a
client for that REST API, so the setup is: `slskd` runs in the background, your
program talks to `soulseek_api`, `soulseek_api` talks to slskd.

You need a real Soulseek account (free, register at slsknet.org or from any client).
Soulseek is a **sharing** network: leeching without sharing gets you banned by other
users' clients, so point slskd's `shares.directories` at a genuine music folder.

## Setup

**1. Install and run slskd**

```bash
brew install slskd          # macOS; or use the Docker image / a release binary
slskd                       # writes a starter config on first run
```

Config lives at `~/.local/share/slskd/slskd.yml`. The parts that matter:

```yaml
soulseek:
  username: your_soulseek_username
  password: your_soulseek_password
shares:
  directories:
    - /Users/you/Music/Shared
directories:
  downloads: /Users/you/Music/Soulseek
web:
  port: 5030
  authentication:
    api_keys:
      my-app:
        key: generate-a-long-random-string-here
        role: readwrite
        cidr: 127.0.0.1/32
```

Restart slskd, then confirm the web UI at http://localhost:5030 shows *Connected*.

**2. Install this package's dependency**

```bash
pip install -r requirements.txt
```

**3. Point the client at slskd**

```bash
cp .env.example .env    # then fill in SLSKD_API_KEY
```

`from_env()` reads plain environment variables. To load a `.env` file, either
`pip install python-dotenv` and call `load_dotenv()` first, or pass the values
directly: `SoulseekClient(url=..., api_key=...)`.

## Check it works

```bash
python -m soulseek_api.cli status
python -m soulseek_api.cli search "boards of canada roygbiv" --audio-only
python -m soulseek_api.cli get "boards of canada roygbiv" --format flac --format mp3
```

## API

### Client

| | |
|---|---|
| `SoulseekClient(url, api_key=, username=, password=)` | Construct directly. |
| `SoulseekClient.from_env(**overrides)` | Read `SLSKD_URL` / `SLSKD_API_KEY` / `SLSKD_USERNAME` / `SLSKD_PASSWORD`. |
| `ping()` | `True` if slskd is up *and* logged in to Soulseek. |
| `state()` | Full daemon state dict. |

### Searching

| | |
|---|---|
| `search(query, timeout=30, audio_only=, extensions=, min_bitrate=)` | Run a search to completion; returns a ranked flat list of `SearchFile`. |
| `start_search(query)` / `wait_for_search(id)` / `get_responses(id)` / `delete_search(id)` | The same thing in pieces, if you want to poll yourself. |
| `filter_files(files, ...)` | Re-filter and re-rank a result list. |
| `best_match(files, prefer_extensions=["flac","mp3"])` | Pick one, honouring format preference. |

Results are ranked free-upload-slot first, then by upload speed, then by shortest
queue — so `files[0]` is the one most likely to actually transfer quickly, not
necessarily the highest quality. Use `prefer_extensions` when quality matters more.

### Downloading

| | |
|---|---|
| `download(file)` | Enqueue one `SearchFile`; returns a `Transfer`. |
| `download_many(files)` | Enqueue a batch, grouped per user. |
| `wait_for_download(transfer, timeout=600, on_progress=)` | Block until done; raises `DownloadFailed` on a bad end state. |
| `download_and_wait(file)` | The two above, combined. |
| `get_downloads(username=None)` | Current transfers. |
| `cancel_download(transfer)` | Cancel and remove. |

Files land in slskd's configured `directories.downloads`, **not** in your process's
working directory — slskd does the writing. Read that path from `state()` if your
program needs to pick the file up afterwards.

### Users

`user_info(username)`, `user_status(username)`, `browse(username)`,
`browse_directory(username, directory)` — the last one is how you grab a whole album
(see `whole_folder()` in `example.py`). Note that Soulseek paths are Windows-style,
so directory and file names join with a backslash.

## Errors

All inherit from `SoulseekError`: `ConnectionFailed` (slskd unreachable),
`AuthenticationFailed` (bad key/credentials), `APIError` (non-2xx, carries
`.status_code`), `SearchTimeout`, `DownloadFailed`.

```python
from soulseek_api import SoulseekClient, SoulseekError

try:
    with SoulseekClient.from_env() as sk:
        ...
except SoulseekError as exc:
    print(f"soulseek unavailable: {exc}")
```

## Notes for when you build on this

- **Searches are slow by nature** — 15–30s is normal; results trickle in from peers.
  Don't call `search()` on a request thread without a timeout budget.
- **Many downloads queue rather than start.** A transfer can sit in `Queued, Remotely`
  for a long time if the uploader is busy. Prefer results with a free slot, and give
  `wait_for_download` a generous timeout.
- **Failures are routine.** Users go offline mid-transfer. For anything unattended,
  catch `DownloadFailed` and retry with the next candidate from the result list.
- The client is synchronous and not thread-safe per instance; use one client per
  thread, or serialise calls.

## Tests

`tests/test_client.py` runs the client against a stub slskd on localhost — no daemon
and no network needed:

```bash
python tests/test_client.py
```
