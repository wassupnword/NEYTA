# uvr-local

Ultimate Vocal Remover as a plain Python module + CLI. Everything lives in this
folder — its own Python, its own venv, its own ffmpeg, its own model cache.
Nothing is installed system-wide, no sudo, no Homebrew.

## Fresh install

```bash
cd uvr-local
./setup.sh
```

That's the whole thing. It downloads `uv` → a standalone CPython 3.11 (the macOS
system Python is 3.9, too old) → the deps → and links a bundled `ffmpeg` into the
venv. Re-running is safe; it skips what's already there. `./setup.sh --check`
verifies an install without changing anything.

First separation additionally downloads that preset's model checkpoints into
`models/` (66 MB for `vocals_fast`, ~300 MB for `vocals`). After that it's offline.

## Use it

```bash
./uvr song.wav                      # vocals + instrumental
./uvr song.wav -p stems -f flac     # drums/bass/other/vocals
./uvr ./library -r -p vocals_fast -o out/
./uvr --list-presets
```

`./uvr` is a wrapper that uses this folder's venv, so it works from any
directory and needs no `activate`. `.venv/bin/python uvr.py …` is equivalent.

From other code:

```python
import sys; sys.path.insert(0, "uvr-local")   # or just run from this folder
from uvr import UVR, separate, split_stems, PRESETS

out = separate("song.wav", preset="vocals")
print(out["vocals"], out["instrumental"])

# Reuse one instance across many files — the model stays loaded in memory.
u = UVR(output_dir="out", output_format="flac")
results = u.separate_batch(["a.wav", "b.wav"], preset="vocals_clean")
```

Everything returns `{stem_name: Path}`, so the bigger project never has to parse
filenames.

## Nothing is ever overwritten

Each track gets its own folder, and a folder that already has files in it is
never written into — the run goes to `<name>-2`, `-3`, … instead:

```
out/
  Song Name/     first run
  Song Name-2/   second run, first is untouched
```

Track names are sanitised, so a `/` in a title can't escape the output folder.
Pass `--overwrite` (or `UVR(overwrite=True)`) to write in place instead.

## Presets

A preset is a **chain** — each step runs on a stem the previous step produced.

| preset | what you get |
| --- | --- |
| `vocals` | vocals + instrumental, best quality (BS-Roformer, ~300 MB) |
| `vocals_fast` | same split, quicker, fine on CPU (MDX-Net Voc_FT) |
| `vocals_clean` | vocals → de-reverb → de-noise, applied to the vocal only |
| `karaoke` | instrumental + lead vs. backing vocals |
| `stems` | drums / bass / other / vocals (htdemucs_ft) |
| `stems6` | + guitar / piano (htdemucs_6s) |
| `dereverb`, `denoise` | cleanup only, no separation |

Add your own by appending to `PRESETS` in `uvr.py`:

```python
PRESETS["my_chain"] = Preset("description", [
    Step("model_bs_roformer_ep_317_sdr_12.9755.ckpt"),
    Step("UVR-DeNoise.pth", feed="vocals", rename={"no noise": "vocals"}),
])
```

`--list-models` prints every checkpoint audio-separator can fetch; `-m <file>`
runs one directly, bypassing presets.

## Layout

```
uvr.py                 the module + CLI  (the only file you need to read)
setup.sh               fresh-install bootstrap, also --check
requirements.txt       direct deps, commented
requirements.lock.txt  full transitive pin for byte-identical rebuilds
bin/                   uv, uvx
python/                standalone CPython 3.11
.venv/                 the virtualenv (+ ffmpeg symlink)
models/                model checkpoints, downloaded on demand
```

Only `uvr.py`, `setup.sh`, `requirements*.txt` and this README are source; the
other four directories are regenerable — delete them and re-run `./setup.sh`.

## Notes

- Apple Silicon: `audio-separator[cpu]` is the right extra. The `[gpu]` extra
  pulls CUDA wheels that don't exist for macOS; swap it in `requirements.txt`
  only on an NVIDIA box.
- `UVR_MODEL_DIR` overrides where checkpoints are cached.
- Progress bars come from audio-separator and go to stderr, so
  `2>/dev/null` gives you clean stem paths on stdout for piping.
