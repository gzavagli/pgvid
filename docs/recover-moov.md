# Recovering a clip with a missing `moov` atom

If the camera loses power while recording (flat battery, cold, a knock on the button), the
MP4 is left without its **`moov` atom**: the index that tells players where every frame is.
The file is mostly intact, but ffprobe/ffmpeg refuse to open it. GoPro's own repair runs on
the camera and doesn't always work.

`pgvid recover` rebuilds a playable video and salvages the telemetry.
Code: [`recover.py`](../pgvid/recover.py).

```bash
pgvid recover GX010123.MP4                 # reference = closest lower-numbered healthy clip
pgvid recover GX010123.MP4 ref=GX010120.MP4
```

## How it works

An MP4 is a sequence of boxes. A cut-off GoPro file has `ftyp`, maybe some others, and then
a huge `mdat` holding the interleaved samples:

- **HEVC video**: each frame is a series of NAL units, each prefixed with a 4-byte length
- **AAC audio** frames
- **GPMF telemetry**: `DEVC` KLV blocks (see [GPMF](gpmf.md))

Without `moov`, nothing says where one sample ends and the next begins, so we walk the
`mdat` byte by byte:

1. **Is this a plausible HEVC NAL?** Read the 4-byte length and check the 2-byte NAL header:
   forbidden bit 0, layer 0, temporal id ≥ 1, NAL type in a valid range, and length within
   bounds. Require the *next* NAL to also look valid (a chain), which rules out random audio
   bytes that happen to pass. Keep it and convert it to Annex-B (start code `00 00 00 01`).
2. **Is this a `DEVC` block?** Copy it to a `.gpmf.bin` file.
3. **Otherwise** (audio, padding), skip ahead one byte at a time until a NAL chain of 3 or
   a `DEVC` shows up again.

The raw HEVC stream has no parameter sets (they lived in `moov`), so the **VPS/SPS/PPS are
taken from a healthy reference clip** recorded with the same resolution/fps settings and
prepended. ffmpeg then remuxes the elementary stream into a new MP4 (`-c copy`, no
re-encode, tagged `hvc1`). `index` picks up the recovered file and its salvaged telemetry
automatically.

## Limitations

- No audio (AAC frames are skipped; their boundaries are much harder to find reliably).
- The last fraction of a second before power loss may be missing or corrupted.
- The reference clip **must** use the same video settings, or decoding fails.
- Written for HEVC GoPro files. H.264 would need a different NAL header check.
