# Beat-synced editing

Cuts that land on the beat make an edit feel intentional. pgvid analyses each track once and
snaps every cut to the music.
Code: [`beats.py`](../pgvid/beats.py), [`edl.py`](../pgvid/edl.py),
[`render.py`](../pgvid/render.py) (audio mix).

## Music analysis (`pgvid beats`)

For every `.mp3`/`.wav` in `music_dir` (named `Title - Artist`, used for the credits):

- **tempo and beats**: librosa's beat tracker (fairly high `tightness`, for steady grids)
- **downbeats**: of the four possible bar phases, the one whose beats carry the most onset
  energy. Simple, and good enough for the 4/4 music typical of these videos.
- **energy curve**: RMS in 0.5 s hops, smoothed over 4 s, normalised
- **drops**: the downbeats with the largest jump between the 4 s of energy before and after.
  These are the moments where the music "kicks in".

## Long video

- Music tracks are placed on the timeline by chapter (`long.music:` lists
  `[track, in-point, chapter index]`). A switch happens 1 s before a chapter card, with a
  2 s crossfade.
- All tracks' beats merge into one **beat grid** for the timeline.
- Each shot gets a nominal length (by category: launches and landings longer, glides
  shorter). Its end is snapped to the nearest beat within 0.7-1.35× the nominal length.
  Section ends (teaser, title, cards, last shot of a chapter) snap to **downbeats**.

## Short

- The track's in-point is chosen so that a **drop lands at 2.0 s**, exactly where the hook
  shot (best gaggle/close pass, with a big caption) cuts to the body. Set `short.drop` to
  pick a different drop.
- Body shots alternate between 4 and 6 beats long.
- The last shot continues the hook clip from 3.5 s *before* the hook point, so when YouTube
  loops the Short, the end flows into the beginning.
- The vertical 9:16 crop pans slowly towards detected gliders (limited pan speed, pulled
  towards the centre).

## Audio mix

- Camera audio per shot: `up` (featured, e.g. launch, vario beeping in a strong thermal),
  `low` or `mute`. Music is **ducked to 55 %** under `up` shots.
- Two-pass loudness: measure the integrated loudness of the mix (EBU R128), apply the exact
  gain to reach **−14 LUFS** (YouTube's reference), then a true-peak limiter.
