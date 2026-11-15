# The telemetry HUD overlay

The HUD in the corner of the flying shots shows **climb rate**, **altitude**, **ground speed**
and the **local clock**, computed from your IGC tracklog. No instrument screen recording is
involved.

Code: [`igc.py`](../pgvid/igc.py) (flight metrics), [`cards.py` `hud_frame`](../pgvid/cards.py)
(drawing) and [`render.py` `_overlay_gen`](../pgvid/render.py) (compositing).

## 1. From IGC fixes to smooth metrics

IGC `B` records give time, lat/lon, pressure altitude and GPS altitude, usually at 1 Hz.
`parse_igc`:

1. Parses fixes, handles midnight rollover, sorts and de-duplicates timestamps.
2. Resamples everything onto a **regular 1 Hz grid** (linear interpolation). Logs with gaps
   or 2-5 s intervals become uniform.
3. Projects lat/lon to local metres (equirectangular around the first fix). That's plenty
   accurate over a flight.
4. Derives:
   - **ground speed**: gradient of the 5 s-smoothed position, ×3.6, smoothed again over 5 s
   - **vario**: gradient of the 9 s-smoothed altitude, smoothed again over 5 s. Raw 1 Hz GPS
     altitude is too noisy for a pleasant needle, and two short box filters keep the lag
     around 3-4 s.
   - **heading** and **turn rate** (deg/s, unwrapped), which [sync](sync-without-gps.md) uses
   - **takeoff/landing**: sustained ground speed > 12 km/h over a 30 s window
5. GPS altitude is used when present, pressure altitude otherwise.

## 2. Mapping video time to tracklog time

Once [sync](sync-without-gps.md) has placed a clip (`start_epoch` = UTC time of the clip's
first frame), the tracklog time of output frame `i` of a shot is:

```
epoch = start_epoch + shot.in + i / 29.97 × shot.speed
```

`Track.at(epoch, field)` interpolates linearly between the 1 Hz samples, so the numbers
change smoothly at 30 fps even though the log is 1 Hz. Sped-up shots (`speed: 1.5`) advance
the telemetry at the same rate as the picture.

## 3. Drawing the panel

`hud_frame` draws a small RGBA panel (820×360 px at 4K, scaled to the output width) with
Pillow:

- a **vario bar**, ±5 m/s, filled green up or red down from a centre line
- the vario value (big, coloured), altitude in m and ground speed in km/h
- a footer with the chapter label ("TASK 2", "FLIGHT 1") and the local clock (`utc_offset_hours`)
- a semi-transparent rounded background so it reads over sky and terrain alike
- a 12-frame fade at both ends of the shot

Fonts come from the bundled Barlow Condensed or your `fonts:` overrides.

## 4. Compositing with ffmpeg without temp files

Writing a PNG per frame for minutes of 4K footage is slow and huge. Instead, frames are
**streamed as raw RGBA into ffmpeg's stdin** as a second input and composited with the
`overlay` filter:

```
ffmpeg -ss IN -t DUR -i clip.MP4 \
       -f rawvideo -pix_fmt rgba -s 820x360 -r 30000/1001 -i - \
       -filter_complex "[0:v]<scale,grade,...>[base];[1:v]format=rgba[ov];[base][ov]overlay=X:Y" ...
```

A writer thread feeds the frames while ffmpeg encodes. When a shot only has the HUD, only
the small panel is streamed, positioned with `overlay=X:Y`. That is almost 30× less data than
full frames. Shots that combine overlays (`lower_third+hud`, `title`, `caption`,
`endscreen`) stream full-size transparent frames.

## 5. When the HUD is shown

The automatic EDL only puts `overlay: hud` on in-flight shots whose clip synced with
`high`/`medium` confidence and which have IGC data at that time. You can add or remove it
per shot in the EDL.

## Using the HUD on its own

```python
from pgvid.igc import parse_igc
from pgvid.cards import hud_frame
tr = parse_igc(Path("flight.igc"))
ep = tr.takeoff + 1800                      # 30 min into the flight
panel, (x, y) = hud_frame(3840, 2160, float(tr.at(ep, "alt")), float(tr.at(ep, "vario")),
                          float(tr.at(ep, "speed")), "14:02:11", "FLIGHT 1")
panel.save("hud.png")
```
