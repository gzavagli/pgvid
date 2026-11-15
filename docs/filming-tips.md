# Filming tips

What makes footage work well with this pipeline (and with viewers).

## Camera

- **Leave telemetry (GPMF) on.** It's on by default on GoPros. Sync depends on the gyro.
- **Keep the low-res proxies (`.LRV`).** Copy them along with the `.MP4` files: analysis and
  previews run on them and are much faster.
- **4K at 29.97/30 fps** is what the pipeline outputs. Other frame rates work, but are
  converted.
- **Linear or wide lens, stabilisation on.** Heavy horizon levelling can weaken the yaw
  signal in some modes. If sync struggles, try a mode without horizon lock.
- **Don't kill the power mid-recording.** If it happens anyway, see [recover](recover-moov.md).
- The clock doesn't need to be right. Set it anyway, it doesn't hurt.

## Mounting

- A **chest or cockpit mount** gives the classic "pilot's view": risers, instrument, wing tips
  and the gaggle ahead. It also rotates with your body, which is ideal for gyro sync.
- Helmet mounts give great views but turn with your head. Sync still works on longer clips
  (head turns are clamped and averaged out), but less reliably on short ones.
- If you want instrument-clock OCR, keep the **instrument screen in the lower part of the
  frame**, with seconds shown on the clock (`HH:MM:SS`), and a font size that survives 4K
  compression.

## Recording

- **Start before launch.** Launch is one of the best moments, and the first thermals give
  sync a clear signature.
- Record long stretches rather than many 10 s snippets. Long clips sync far better.
- Point the camera **slightly up**. Sky, clouds and other wings are the interesting part;
  your harness isn't.
- In a gaggle, keep recording through the climb: close passes come out of the scoring
  automatically.
- Vario beeps make great audio for strong-thermal shots (featured with `audio: up`).

## Tracklogs

- Log at **1 s** if your instrument allows it.
- One IGC per day. Make sure every flying day you filmed has its tracklog.

## Music

- Use tracks you have the rights to use on YouTube. Name the files `Title - Artist.mp3`
  (use `_` for `&`) and the credits are written for you.
- Tracks with a clear beat and a distinct drop work best for the Short.
