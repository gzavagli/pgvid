"""Highlight report (Checkpoint 1): per-task timeline plots, contact sheets, sync table."""
from __future__ import annotations

import datetime as dt
import json
import shutil
import subprocess
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
from PIL import Image, ImageDraw  # noqa: E402

from .cards import font  # noqa: E402
from .config import Project  # noqa: E402
from .igc import load_tracks  # noqa: E402
from .index import load_index  # noqa: E402
from .score import CATEGORIES  # noqa: E402
from .sync import load_sync  # noqa: E402
from .tasks import chapters  # noqa: E402

CAT_COL = {"launch": "#ffffff", "gaggle": "#ff7a1a", "close_pass": "#ff2d55", "strong_thermal": "#34c759",
           "fast_glide": "#0a84ff", "clouds": "#bf5af2", "scenery": "#ffd60a", "goal_landing": "#64d2ff"}
CONF_COL = {"high": "#34c759", "medium": "#ffd60a", "low": "#ff453a"}


def thumb(src: str, t: float, w: int = 480) -> Image.Image:
    r = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{max(t, 0):.2f}", "-i", src, "-frames:v", "1",
                        "-vf", f"scale={w}:-2", "-f", "image2pipe", "-vcodec", "png", "-"], capture_output=True)
    from io import BytesIO
    try:
        return Image.open(BytesIO(r.stdout)).convert("RGB")
    except Exception:
        return Image.new("RGB", (w, w * 9 // 16), (40, 40, 40))


def contact_sheet(items: list[dict], index: dict, cols: int = 4, w: int = 480) -> Image.Image:
    h = w * 9 // 16
    rows = (len(items) + cols - 1) // cols
    sheet = Image.new("RGB", (cols * w, rows * (h + 46)), (18, 18, 18))
    d = ImageDraw.Draw(sheet)
    f = font("demi", 24)
    for i, it in enumerate(items):
        e = index[it["clip"]]
        src = e["proxy"] or e["path"]
        im = thumb(src, it["start"] + it["dur"] / 2, w)
        x, y = (i % cols) * w, (i // cols) * (h + 46)
        sheet.paste(im.resize((w, h)), (x, y))
        col = CAT_COL.get(it["category"], "#fff")
        d.rectangle((x, y, x + 10, y + h), fill=col)
        d.text((x + 6, y + h + 4), f"#{i + 1} {it['clip'][2:8]} @{int(it['start'])//60}:{int(it['start'])%60:02d}",
               font=f, fill=(240, 240, 240))
        d.text((x + 6, y + h + 4), "", font=f)
        lab = f"{it['category']} {it['score']:.2f}"
        d.text((x + w - d.textlength(lab, font=f) - 8, y + h + 4), lab, font=f, fill=col)
    return sheet


def timeline_plot(prj: Project, day: int, track, sync: dict, segs: list[dict], tz, out: Path) -> None:
    fig, ax = plt.subplots(figsize=(16, 4.2), dpi=110)
    fig.patch.set_facecolor("#111")
    ax.set_facecolor("#111")
    t = np.array([dt.datetime.fromtimestamp(x, tz) for x in track.t[::5]])
    ax.plot(t, track.alt[::5], color="#bbb", lw=1)
    vv = track.vario[::5]
    ax.scatter(t, track.alt[::5], c=np.clip(vv, -3, 4), cmap="RdYlGn", s=2, vmin=-3, vmax=4)
    y0 = np.nanmin(track.alt) - 250
    for c in sync.values():
        if c["day"] != day or c["start_epoch"] is None:
            continue
        a = dt.datetime.fromtimestamp(c["start_epoch"], tz)
        b = dt.datetime.fromtimestamp(c["start_epoch"] + c["duration"], tz)
        ax.axvspan(a, b, ymin=0, ymax=0.06, color=CONF_COL[c["confidence"]], alpha=0.9)
    for s in segs:
        if s["epoch"] is None:
            continue
        x = dt.datetime.fromtimestamp(s["epoch"], tz)
        ax.scatter([x], [s["alt"] or y0 + 300], marker="v", s=60, color=CAT_COL[s["category"]],
                   edgecolor="black", zorder=5)
    ax.set_ylim(y0, np.nanmax(track.alt) + 150)
    ax.tick_params(colors="#ccc")
    for sp in ax.spines.values():
        sp.set_color("#444")
    import matplotlib.dates as mdates
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M", tz=tz))
    ax.set_ylabel("altitude (m)", color="#ccc")
    handles = [plt.Line2D([], [], marker="v", ls="", color=CAT_COL[k], label=k) for k in CATEGORIES]
    handles += [plt.Rectangle((0, 0), 1, 1, color=CONF_COL[k], label=f"clip ({k} sync)") for k in CONF_COL]
    ax.legend(handles=handles, loc="upper center", bbox_to_anchor=(0.5, -0.12), ncol=11, fontsize=7,
              facecolor="#222", labelcolor="#ddd", edgecolor="#444")
    fig.tight_layout()
    fig.savefig(out, facecolor=fig.get_facecolor())
    plt.close(fig)


def run(prj: Project, force: bool = False, args: list[str] | None = None) -> None:
    tz = dt.timezone(dt.timedelta(hours=prj["utc_offset_hours"]))
    index = load_index(prj)
    sync = load_sync(prj)
    tracks = load_tracks(prj.igc_dir)
    chaps = chapters(prj, tracks)
    segs = json.loads(prj.work("segments.json").read_text())
    art = Path(args[0]) if args else prj.out("report")
    art.mkdir(parents=True, exist_ok=True)
    md = [f"# Highlight report: {prj['event'] or prj['name']}", ""]
    tot = sum(c["duration"] for c in sync.values())
    conf = {k: sum(c["duration"] for c in sync.values() if c["confidence"] == k) for k in CONF_COL}
    md += [f"Footage: **{len(sync)} clips, {tot/60:.0f} min**. Sync confidence by duration: "
           + ", ".join(f"{k} {v/60:.0f} min" for k, v in conf.items()), ""]
    for day, (tr, chap) in enumerate(zip(tracks, chaps)):
        task = chap["task"] or {}
        tag = f"d{day + 1}"
        dsegs = [s for s in segs if s["day"] == day]
        nclips = [c for c in sync.values() if c["day"] == day]
        md += [f"## {chap['label'].title()}: {chap['date']} · {chap.get('distance_km')} km",
               f"{len(nclips)} clips, {sum(c['duration'] for c in nclips)/60:.0f} min of footage"
               + (f" · result #{task['result']['rank']}" if task.get("result") else ""), ""]
        tp = art / f"timeline_{tag}.png"
        timeline_plot(prj, day, tr, sync, dsegs, tz, tp)
        md += [f"![{chap['label'].title()} timeline]({tp.name})", ""]
        top = sorted(dsegs, key=lambda s: -s["score"])
        # best per category first, then fill by score
        pick, seen = [], set()
        for cat in CATEGORIES:
            b = next((s for s in top if s["category"] == cat), None)
            if b:
                pick.append(b)
                seen.add(id(b))
        pick += [s for s in top if id(s) not in seen][: 16 - len(pick)]
        if pick:
            cs = art / f"contact_{tag}.jpg"
            contact_sheet(pick, index).save(cs, quality=85)
            md += [f"![{chap['label'].title()} top candidates]({cs.name})", ""]
            md += ["| # | clip | at | category | score | alt | vario | speed | gliders |",
                   "|---|---|---|---|---|---|---|---|---|"]
            for i, s in enumerate(pick, 1):
                md.append(f"| {i} | {s['clip']} | {int(s['start'])//60}:{int(s['start'])%60:02d} | {s['category']} | "
                          f"{s['score']:.2f} | {s['alt'] or '-'} | {s['vario'] or '-'} | {s['speed'] or '-'} | "
                          f"{s['gliders'] or '-'} |")
            md.append("")
    md += ["## Clip sync table", "", "| clip | dur | day | local start | method | confidence |", "|---|---|---|---|---|---|"]
    for c in sorted(sync.values(), key=lambda c: index[c["clip"]]["order"]):
        st = dt.datetime.fromtimestamp(c["start_epoch"], tz).strftime("%H:%M:%S") if c["start_epoch"] else "-"
        md.append(f"| {c['clip']} | {c['duration']:.0f}s | {c['date'] or '-'} | {st} | {c['method']} | {c['confidence']} |")
    (art / "highlight_report.md").write_text("\n".join(md))
    print("report ->", art / "highlight_report.md")
