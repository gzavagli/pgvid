# Highlight scoring

Hours of cockpit footage contain maybe ten minutes worth showing. `analyze` + `score` find
candidate segments in eight categories. The [report](pipeline.md#the-two-checkpoints) lets
you check them before anything gets cut.
Code: [`analyze.py`](../pgvid/analyze.py), [`score.py`](../pgvid/score.py).

## Features

Computed from the low-resolution LRV proxies (fast), at 2 fps on 384×216 frames:

| Feature | How |
|---|---|
| `bright`, `contrast`, `sat` | HSV value mean, grey std-dev, saturation mean |
| `sky` | fraction of blue-sky pixels |
| `cloud` | fraction of bright, unsaturated pixels in the upper half |
| `dark` | fraction of very dark pixels: a glove or the harness filling the frame |
| `green` | vegetation/ground fill |
| `flow` | median optical flow (Farnebäck) outside the cockpit area: speed over terrain |
| `blobs` | small, saturated, non-blue blobs surrounded by sky: probable gliders |
| `sharp` | Laplacian variance (blur/defocus) |
| `phash` | 64-bit DCT perceptual hash, used to drop near-duplicate shots |
| `rms`, `beep`, `wind` | audio level, 1-4 kHz band ratio (vario beeps), < 300 Hz ratio (wind noise) |

**Glider detection** (optional `[detect]` extra) runs an off-the-shelf COCO object detector
at 1 fps. Paragliders reliably come out as *kite* (also *bird* or *airplane*), so those three
classes are kept at a low confidence threshold, and detections in the bottom 30 % of the
frame (your own wing tips and risers) are dropped. It yields the glider count, the largest
glider's area and an area-weighted centroid, which the Short's vertical crop follows.
Without detection, the `blobs` heuristic stands in.

From [sync](sync-without-gps.md) and the IGC, each second also gets altitude, vario, ground
speed, turn rate, time since takeoff, time to landing and relative altitude within the day.

## Categories

All features are merged on a 1 Hz timeline per clip and normalised **across the whole
footage set** (5th-95th percentile → 0..1), so "high contrast" means high for this trip.
Each category is a simple, readable formula, multiplied by a quality factor that penalises
darkness, blur and shake:

| Category | Roughly |
|---|---|
| `launch` | from 45 s before to 40 s after takeoff (needs good sync), favouring motion/jerk |
| `gaggle` | in flight, many gliders (+ blobs), bonus when turning |
| `close_pass` | a glider taking up a large part of the frame |
| `strong_thermal` | vario > 1.5 m/s, scaling to +4.5, while turning, bonus with gliders around |
| `fast_glide` | ground speed > 35 km/h, not turning, high optical flow |
| `clouds` | cloud fraction, high in the day's altitude band, contrast |
| `scenery` | contrast, saturation, vegetation, balanced sky fraction |
| `goal_landing` | from 100 s before to 20 s after landing |

## Segments

For each clip and category, a 5 s moving average is taken and up to 4 peaks (25 s apart,
score > 0.15) become candidate segments. Per day and category, the best 8 are kept after
dropping perceptual-hash duplicates. The EDL builder then picks from these with
per-category caps, a per-clip cap and overlap/duplicate checks. Each chapter must include
a launch, a goal/landing, a gaggle and a strong thermal when they exist.

## Steering it

The automatic picks are a starting point. In the project YAML:

```yaml
pins:      # force a segment in (clip seconds; the shot is centred on the window)
  - {clip: GX010262.MP4, start: 23, dur: 10, category: launch, score: 1.0, note: "reverse launch"}
exclude:   # never use these ranges
  - {clip: GX010212.MP4, start: 0, end: 61}
```

Re-run `score` → `report` → `edl --force`, or simply edit the EDL.
