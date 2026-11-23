#!/usr/bin/env python3
"""Slide-extraction for one lecture video. Designed to run on a GitHub Actions runner.

Usage: python3 extract.py <video_number>

Writes to /tmp/out/<n>/:
  status.json   - stage-by-stage status (always written, even on failure)
  slides.json   - unique slides with timestamps, OCR text+confidence, aligned speech
  slides/*.jpg  - downscaled images of each unique slide
  subs.srt      - teacher narration (if subtitles available)
"""
import glob
import json
import re
import subprocess
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(line_buffering=True)

REPO = Path(__file__).resolve().parent
N = int(sys.argv[1])
VIDEOS = {v["n"]: v for v in json.load(open(REPO / "videos.json"))}
META = VIDEOS[N]
YID = META["id"]
URL = f"https://www.youtube.com/watch?v={YID}"
OUT = Path(f"/tmp/out/{N}")
(OUT / "slides").mkdir(parents=True, exist_ok=True)

FPS_SAMPLE = 2          # seconds between sampled frames
DHASH_SIZE = 16         # 16x16 dHash = 256 bits
HAMMING_NEW_SLIDE = 10  # hamming distance above which a frame is a NEW slide
SLIDE_MAX_W = 640       # saved slide image width cap (audit images; OCR runs on full-res frame)

# yt-dlp attempt ladder: None = default clients (uses bgutil POT server on :4416)
# NOTE: fetch_pot=always is REQUIRED - without it yt-dlp skips PO token fetching
# entirely and datacenter IPs get the bot wall (verified experimentally).
CLIENT_LADDER = [
    None,       # default clients + POT
    "tv",
    "mweb",
    "android_vr",
    "tv_embedded",
    "web_safari",
    "ios",
]


def ytdlp_args(client):
    ea = "youtube:fetch_pot=always"
    if client:
        ea += f";player_client={client}"
    return ["--extractor-args", ea]


def save_status(**kw):
    st = {"n": N, "id": YID, "title": META["title"], "dur": META["dur"],
          "paper": META["paper"], "ts": time.strftime("%Y-%m-%d %H:%M:%S")}
    st.update(kw)
    json.dump(st, open(OUT / "status.json", "w"), ensure_ascii=False, indent=1)


def sh(cmd, timeout=3000):
    t0 = time.time()
    print(f"$ {' '.join(cmd[:6])} ...", flush=True)
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    r.elapsed = round(time.time() - t0, 1)
    return r


def tail(s, n=500):
    return (s or "").strip()[-n:]


# ------------------------------------------------------------------ download
def download():
    """Try the client ladder. Returns (video_path|None, srt_path|None, log)."""
    log = []
    for i, client in enumerate(CLIENT_LADDER):
        label = client or "default+POT"
        for f in glob.glob("/tmp/vid*"):
            Path(f).unlink(missing_ok=True)
        cmd = ["yt-dlp", "--js-runtimes", "node",
               "-f", "bv*[height<=720][ext=mp4]+ba[ext=m4a]/b[height<=720][ext=mp4]/b[height<=720]/b",
               "--merge-output-format", "mp4",
               "--write-auto-subs", "--write-subs", "--sub-langs", "en.*,hi",
               "--convert-subs", "srt",
               "-o", "/tmp/vid.%(ext)s", "--no-playlist",
               "--retries", "3", "--socket-timeout", "30"] + ytdlp_args(client) + [URL]
        r = sh(cmd, 3000)
        mp4 = next((p for p in ["/tmp/vid.mp4", "/tmp/vid.mkv", "/tmp/vid.webm"] if Path(p).exists()), None)
        srt = next((p for p in sorted(glob.glob("/tmp/vid*.srt"))), None)
        ok = mp4 and Path(mp4).stat().st_size > 100_000
        log.append({"attempt": i, "client": label, "rc": r.returncode,
                    "video": bool(ok), "subs": bool(srt), "secs": r.elapsed,
                    "err": tail(r.stderr, 400) if not ok else ""})
        print(f"  attempt {i} ({label}): rc={r.returncode} video={bool(ok)} subs={bool(srt)} in {r.elapsed}s", flush=True)
        if not ok and "Sign in to confirm" in (r.stderr or ""):
            print("  (bot wall on this client, trying next...)", flush=True)
        if ok:
            return mp4, srt, log
    # full ladder failed for video: one subs-only pass as a last resort
    for client in CLIENT_LADDER[:4]:
        label = "subs-only:" + (client or "default")
        cmd = ["yt-dlp", "--skip-download", "--write-auto-subs", "--write-subs",
               "--sub-langs", "en.*,hi", "--convert-subs", "srt",
               "-o", "/tmp/vid.%(ext)s", "--no-playlist", "--retries", "3"] + ytdlp_args(client) + [URL]
        r = sh(cmd, 600)
        srt = next((p for p in sorted(glob.glob("/tmp/vid*.srt"))), None)
        log.append({"attempt": "subs", "client": label, "rc": r.returncode,
                    "video": False, "subs": bool(srt), "secs": r.elapsed,
                    "err": tail(r.stderr, 400) if not srt else ""})
        if srt:
            print(f"  {label}: got subtitles without video", flush=True)
            return None, srt, log
    return None, None, log


# --------------------------------------------------------------- vision part
def dhash(img):
    from PIL import Image
    g = img.convert("L").resize((DHASH_SIZE + 1, DHASH_SIZE), Image.LANCZOS)
    px = list(g.getdata())
    bits = []
    for row in range(DHASH_SIZE):
        for col in range(DHASH_SIZE):
            bits.append(1 if px[row * (DHASH_SIZE + 1) + col] > px[row * (DHASH_SIZE + 1) + col + 1] else 0)
    return bits


def hamming(a, b):
    return sum(x != y for x, y in zip(a, b))


def ocr_tsv(fp):
    r = subprocess.run(["tesseract", fp, "stdout", "--psm", "3", "-l", "eng", "tsv"],
                       capture_output=True, text=True, timeout=120)
    confs, parts, last_key = [], [], None
    for line in r.stdout.splitlines()[1:]:
        p = line.split("\t")
        if len(p) == 12 and p[0] == "5":
            c = float(p[10])
            if c < 0:
                continue
            key = (p[2], p[3], p[4])  # block/par/line
            if last_key is not None and key != last_key:
                parts.append("\n")
            parts.append(p[11])
            last_key = key
            confs.append(c)
    text = re.sub(r"[ \t]+\n", "\n", " ".join(parts)).replace(" \n ", "\n").strip()
    mean_conf = round(sum(confs) / len(confs), 1) if confs else None
    return text, mean_conf


def frames_dedupe_ocr(mp4):
    from PIL import Image
    Path("/tmp/frames").mkdir(exist_ok=True)
    r = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", mp4,
                        "-vf", f"fps=1/{FPS_SAMPLE}", "-q:v", "3", "/tmp/frames/f_%06d.jpg"],
                       capture_output=True, text=True, timeout=3600)
    if r.returncode != 0:
        raise RuntimeError("ffmpeg failed: " + tail(r.stderr, 300))
    frames = sorted(glob.glob("/tmp/frames/f_*.jpg"))
    print(f"  sampled {len(frames)} frames", flush=True)
    slides = []
    for fp in frames:
        ts = round(int(re.search(r"f_(\d+)", fp).group(1)) * FPS_SAMPLE, 1)
        img = Image.open(fp)
        h = dhash(img)
        dup = None
        for s in slides:
            if hamming(h, s["_hash"]) < HAMMING_NEW_SLIDE:
                dup = s
                break
        if dup is not None:
            dup["also_at"].append(ts)
            continue
        idx = len(slides)
        im2 = img.copy()
        im2.thumbnail((SLIDE_MAX_W, SLIDE_MAX_W))
        sp = OUT / "slides" / f"slide_{idx:03d}.jpg"
        im2.save(sp, "JPEG", quality=70)
        text, conf = ocr_tsv(fp)
        slides.append({"i": idx, "t": ts, "img": sp.name, "text": text,
                       "conf": conf, "also_at": [], "_hash": h})
        print(f"  slide {idx:03d} @ {ts}s conf={conf}", flush=True)
    for s in slides:
        del s["_hash"]
    return slides, len(frames)


# ------------------------------------------------------------------- subs
def parse_srt(fp):
    txt = open(fp, encoding="utf-8", errors="replace").read()
    out = []
    for block in re.split(r"\n\s*\n", txt):
        m = re.search(r"(\d+):(\d+):(\d+)[,.](\d+)\s*-->\s*(\d+):(\d+):(\d+)", block)
        if not m:
            continue
        g = [int(x) for x in m.groups()]
        start = g[0] * 3600 + g[1] * 60 + g[2] + g[3] / 1000.0
        lines = [l.strip() for l in block.splitlines() if "-->" not in l and not l.strip().isdigit()]
        text = " ".join(l for l in lines if l)
        if text:
            out.append({"start": round(start, 1), "text": text})
    return out


def align_speech(slides, subs):
    """Attach to each slide the narration spoken while it was on screen."""
    for i, s in enumerate(slides):
        t_end = slides[i + 1]["t"] if i + 1 < len(slides) else (META["dur"] or s["t"] + 300)
        s["speech"] = " ".join(u["text"] for u in subs if s["t"] <= u["start"] < t_end)


# ------------------------------------------------------------------- main
def main():
    t0 = time.time()
    save_status(stage="download", state="running")
    mp4, srt, dlog = download()
    save_status(stage="download", state="done", video=bool(mp4), subs=bool(srt), attempts=dlog)

    subs = []
    if srt:
        subs = parse_srt(srt)
        (OUT / "subs.srt").write_text(Path(srt).read_text(encoding="utf-8", errors="replace"), encoding="utf-8")
        print(f"  parsed {len(subs)} subtitle lines", flush=True)

    slides = []
    if mp4:
        try:
            slides, n_frames = frames_dedupe_ocr(mp4)
        except Exception as e:
            save_status(stage="ocr", state="error", error=str(e)[:400])
            print("OCR stage failed:", e, flush=True)
    if slides:
        align_speech(slides, subs)
        json.dump(slides, open(OUT / "slides.json", "w"), ensure_ascii=False, indent=1)
        mean_conf = round(sum(s["conf"] or 0 for s in slides) / len(slides), 1)
        save_status(stage="done", state="ok", n_slides=len(slides),
                    n_subs_lines=len(subs), mean_ocr_conf=mean_conf,
                    total_secs=round(time.time() - t0, 1))
        print(f"DONE video {N}: {len(slides)} slides, {len(subs)} sub lines, conf {mean_conf}", flush=True)
    elif srt:
        save_status(stage="done", state="subs_only", n_slides=0,
                    n_subs_lines=len(subs), total_secs=round(time.time() - t0, 1))
        print(f"DONE video {N}: subtitles only (video download failed)", flush=True)
    else:
        save_status(stage="done", state="failed", attempts=dlog,
                    error=tail(dlog[-1].get("err", ""), 400) if dlog else "no attempts",
                    total_secs=round(time.time() - t0, 1))
        print(f"FAILED video {N}: no video, no subs", flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        import traceback
        traceback.print_exc()
        save_status(stage="crash", state="error", error=str(e)[:400])
