# pgvid

**Paragliding footage + IGC tracklogs → YouTube videos with a flight-telemetry HUD.**

`pgvid` turns a pile of raw GoPro clips and your IGC tracklogs into:

- a 16:9 4K highlight video with a chapter per day/task, animated task maps and a live
  **altitude / climb rate / ground speed HUD** taken from the tracklog,
- a 30 s vertical Short cut to the beat of the music,
- the YouTube package: titles, description with chapters and credits, tags, thumbnails.

It's a pipeline of small steps with two human checkpoints. You review a highlight report,
then 720p previews, and you can hand-edit the edit decision list (EDL) before the final render.

▶ **Example:** [a competition video made with pgvid](https://youtu.be/J0O7xMWbpfY)

> [!NOTE]
> Shared as-is, best effort. It was built for one person's workflow and is macOS-first.
> Issues and PRs are welcome, but there's no promised response time.

## What it needs

| | |
|---|---|
| Camera | GoPro with GPMF telemetry (gyro/accelerometer), used to sync clips to the tracklog |
| Tracklogs | One IGC file per flying day (any flight instrument) |
| Tools | Python ≥ 3.10, `ffmpeg`/`ffprobe` on `PATH` |
| OS | macOS is the tested platform. Instrument-clock OCR is macOS-only; everything else is plain Python + ffmpeg |
| Music | Royalty-free tracks you have the rights to use |

The camera clock doesn't need to be set and no camera GPS is needed. Clips are placed on the
tracklog timeline by correlating the camera's gyro with the turn rate in the IGC
([how](docs/sync-without-gps.md)).

## Install

Clone this repository, then from its folder:

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e .              # core
pip install -e ".[ocr]"       # + instrument-clock OCR (macOS)
pip install -e ".[detect]"    # + glider detection (see licence note)
pip install -e ".[dev]"       # + tests
```

Extras can be combined, e.g. `pip install -e ".[ocr,detect]"`.

## Quick start

```bash
cp examples/example_project.yaml myflights.yaml   # edit paths, names, music, competition
export PGVID_PROJECT=myflights.yaml

pgvid index                 # ffprobe + GPMF telemetry per clip
pgvid tasks                 # competition tasks (optional; see `competition:` in the YAML)
pgvid ocr                   # optional, macOS: read the instrument clock in frame
pgvid sync                  # place every clip on the IGC timeline
pgvid analyze               # visual/audio features (+ glider detection if installed)
pgvid score                 # highlight candidates per category
pgvid report                # checkpoint 1: output/<name>/report/highlight_report.md
pgvid beats                 # music tempo / beats / drops
pgvid edl                   # work/<name>/edl/{long,short}.yaml (hand-editable)
pgvid render long short     # checkpoint 2: 720p previews
pgvid render long short final   # 4K / 1080x1920 masters
pgvid metadata              # titles, descriptions, chapters, tags, thumbnails
```

Every step caches its output. Re-run with `--force` to recompute. Rendering only rebuilds the
EDL segments that changed.

## How it works

| Topic | Guide |
|---|---|
| Pipeline overview, checkpoints, EDL editing, config reference | [docs/pipeline.md](docs/pipeline.md) |
| The telemetry HUD overlay | [docs/hud-overlay.md](docs/hud-overlay.md) |
| Syncing a camera with no GPS / unset clock | [docs/sync-without-gps.md](docs/sync-without-gps.md) |
| Reading GoPro GPMF telemetry without the SDK | [docs/gpmf.md](docs/gpmf.md) |
| Recovering clips with a missing `moov` atom | [docs/recover-moov.md](docs/recover-moov.md) |
| Highlight scoring | [docs/highlight-scoring.md](docs/highlight-scoring.md) |
| Beat-synced editing | [docs/beat-sync.md](docs/beat-sync.md) |
| Filming tips | [docs/filming-tips.md](docs/filming-tips.md) |

## Licences

- Code: [MIT](LICENSE).
- Bundled fonts: Barlow Condensed, [SIL OFL 1.1](pgvid/assets/fonts/OFL.txt).
- Optional `[detect]` extra: [ultralytics](https://github.com/ultralytics/ultralytics) is
  **AGPL-3.0**. pgvid only imports it if you install the extra. If you distribute something
  built with it, AGPL terms apply to that combination.
- Map tiles: © OpenStreetMap contributors, SRTM | map style © OpenTopoMap (CC-BY-SA). The
  attribution is drawn on the maps and added to the video description. Respect the
  [tile usage policy](https://opentopomap.org/about) (tiles are cached locally, so each one is
  fetched once).
- Competition data from Airtribune is scraped from public result pages and cached. Use it
  considerately.
