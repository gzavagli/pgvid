# Syncing a camera with no GPS (or an unset clock) to the tracklog

Small action cameras often have no GPS, and their clock is rarely set correctly, so file
timestamps are useless. But the HUD, the task-map progress and the chapter assignment all
need to know **when** each clip was filmed, to the second.

pgvid combines two independent signals. Code: [`sync.py`](../pgvid/sync.py),
[`ocr.py`](../pgvid/ocr.py), [`index.py`](../pgvid/index.py).

## Signal 1: the camera's gyro vs the tracklog's turn rate

When you circle in a thermal, both the camera and the glider rotate at about 15-25 °/s
for minutes at a time. The IGC tells you exactly when you were turning, in which direction
and how fast. The camera's gyro says the same thing, at 200 Hz.

**Camera side** (`index.reduce_telemetry`):

1. The accelerometer, low-passed over ~2 s, gives the apparent **"up" vector**. In a steady
   turn that's the direction of gravity plus centripetal acceleration, i.e. roughly the
   pilot's yaw axis, whatever the camera mount angle.
2. Projecting the gyro vector onto that axis gives the **yaw rate about the vertical**,
   independent of how the camera is mounted.
3. Averaged to 10 Hz, then 1 Hz, clamped to ±45 °/s (head turns cause big spikes) and
   smoothed over 5 s.

**Tracklog side**: the IGC turn rate (see [HUD doc](hud-overlay.md)), clamped the same way
and zeroed before takeoff and after landing.

**Matching**: normalised cross-correlation (Pearson r of the clip against every window of the
day's turn rate), computed with FFTs and running sums, so a 2-minute clip against a
6-hour flight takes milliseconds. Windows where the tracklog has essentially no turning are
ignored, since they would correlate with anything. A score `z = r·√(L/8)` rewards long clips
with good correlation. The top 6 peaks (at least 40 s apart) for each day become
**candidates**.

The sign of the gyro axis depends on the mount, so both signs are tried for the whole
footage set and the better total wins.

## Signal 2: the instrument clock in frame (optional, macOS)

If your flight instrument screen is visible in the shot and shows `HH:MM:SS`, `pgvid ocr`
grabs ~3-14 frames per clip, crops the lower part of the frame, and runs Apple's Vision
text recognition on them. Each reading gives `clip start = clock − t`.
Readings that agree within 3 s form a consensus (at least 2 needed). That anchors the clip's
time of day exactly. The gyro correlation still decides which **day** it belongs to.

Configure the crop and accepted hours in the `ocr:` section. Without OCR, everything relies
on signal 1, which works well for clips with some circling in them.

## Putting it together: a global placement

Clips are taken in recording order (file numbers). GoPro splits long recordings into
chapters (`GX01nnnn`, `GX02nnnn`, ...), which are concatenated and placed as one unit.
Every recording then picks one candidate (day, start time) or "unplaced", by **dynamic
programming** over the recordings:

- placements must not overlap (2 s tolerance) and time must move forward
- OCR-anchored candidates get a large bonus
- states are pruned to the Pareto front of (end time, total score), which keeps it fast

Recordings that still can't be placed (ground footage before launch, no turning) are put
between their neighbours and marked `low` confidence.

## Confidence

| | Meaning | Used for HUD |
|---|---|---|
| `high` | OCR consensus with ≥ 2 readings | yes |
| `medium` | single OCR reading, or strong gyro correlation (z > 5) | yes |
| `low` | weak correlation or interpolated | no |

`pgvid sync` prints one line per clip, and the report has a sync table and timeline plots
showing where every clip landed on the altitude profile. That's the place to spot a wrong
placement.

## Tips

- One IGC file per day, from whatever instrument you fly with. 1 s logging is best.
- Start recording before launch: launch and early thermals give strong, unambiguous
  turn-rate signatures.
- Very short clips (< 30 s) with no turning can't be placed by gyro alone. OCR helps, or
  just exclude them.
