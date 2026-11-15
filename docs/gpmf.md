# Reading GoPro GPMF telemetry without the SDK

GoPro cameras record sensor data (gyro, accelerometer, gravity vector, orientation, ISO,
shutter, plus GPS on models that have it) in a separate **`gpmd` data track** inside the MP4.
The format is documented in GoPro's open `gpmf-parser` repository, but you don't need the
C SDK: about 100 lines of Python ([`gpmf.py`](../pgvid/gpmf.py)) cover the numeric streams.

## 1. Extract the data track with ffmpeg

```bash
ffprobe -v error -select_streams d -show_entries stream=index,codec_tag_string -of csv=p=0 GX010001.MP4
# 3,gpmd
ffmpeg -v error -i GX010001.MP4 -map 0:3 -c copy -f data - > telemetry.bin
```

This gives the concatenated payloads, one per ~1 s of video.

## 2. KLV structure

Everything is **Key-Length-Value**, big-endian, 32-bit aligned:

```
| key (4 chars) | type (1 char) | struct size (1 byte) | repeat (uint16) | data ... | pad to 4 |
```

- `type` is a C-like type char (`s` int16, `S` uint16, `l` int32, `f` float, `J` uint64, ...),
  or `\0` for a nested container.
- payload size = struct size × repeat. A struct can hold several values (GYRO: 3 × int16 per sample).
- Containers nest: `DEVC` (device) → `STRM` (stream) → the actual keys.

Inside each `STRM`, the sticky metadata comes before the samples:

| Key | Meaning |
|---|---|
| `STMP` | timestamp of the first sample in this payload, in µs |
| `SCAL` | divisor(s) to convert raw integers to SI units |
| `ORIN` | axis order, e.g. `ZXY` |
| `GYRO`, `ACCL`, `GRAV`, `CORI`, ... | the samples |

## 3. Timestamps per sample

A payload holds N samples (e.g. ~200 GYRO samples) with a single `STMP`. pgvid spreads them
evenly between this payload's `STMP` and the next one's:

```
t_i = t0 + (t1 − t0) · i / N
```

The last payload extrapolates with the previous rate. The result for each stream is
`{"t": seconds-from-clip-start, "v": (N, k) float array}`.

## 4. Use it

```python
from pgvid import gpmf
tel = gpmf.load_clip_telemetry("GX010001.MP4")
g = tel["GYRO"]            # rad/s
print(len(g["t"]) / g["t"][-1], "Hz")   # ~200 on recent models
```

[`index.py`](../pgvid/index.py) reduces these to 10 Hz signals: yaw rate about the
apparent vertical, gyro magnitude, high-frequency shake and accelerometer jerk. Sync and
highlight scoring use those.

## Gotchas

- Axis order and sign conventions differ between models (`ORIN`). pgvid avoids depending on
  them by projecting onto the measured "up" vector, and tries both signs during sync.
- A clip whose recording was cut off (battery died) has no `moov` atom, so ffprobe can't see
  the `gpmd` track. The [recover](recover-moov.md) step salvages the `DEVC` blocks directly
  from the raw `mdat` instead.
