# socio-extract

Automated slide-extraction pipeline for the **Study IAS "Sociology Optional Super Series"**
playlist (125 lecture videos). Runs on GitHub Actions runners (datacenter-proof via
PO tokens), commits one `v/NNN` branch per video with:

```
out/<n>/status.json   stage-by-stage run status
out/<n>/slides.json   unique slides: timestamp, OCR text + confidence, aligned teacher speech
out/<n>/slides/*.jpg  audit image of each unique slide (640px)
out/<n>/subs.srt      teacher narration (auto/manual subtitles)
out/<n>/extract.log   full run log
```

## How it works

1. Edit `queue/batch.json` (list of video numbers from `videos.json`) and push to the
   default branch (or trigger via *Run workflow*).
2. The `setup` job posts a heartbeat to the `status` branch, then computes the pending
   matrix = batch minus already-done `v/*` branches.
3. Up to 6 parallel `extract` jobs each: start a local PO-token provider
   (bgutil, Docker), download the video at <=720p with `yt-dlp --extractor-args
   youtube:fetch_pot=always` (client ladder fallback: tv, mweb, android_vr,
   tv_embedded, web_safari, ios), sample a frame every 2 s, deduplicate slides by
   16x16 dHash (Hamming <= 10), OCR each unique slide (tesseract, with confidence),
   and align subtitle text to the slide it was spoken over.
4. Results are committed to branch `v/NNN` (zero-padded). Re-runs skip done videos
   unless `force_all`/`force` is set in `queue/batch.json`.

## Video numbering

`videos.json` carries all 125 lectures (n = playlist position). Paper 1 = n 2-92
(91 lectures), Paper 2 = n 93-126 (34 lectures). n 1/127/128 are non-lecture videos
and are never extracted.

## Repo layout

```
.github/workflows/extract.yml   the workflow (trigger: push to default branch)
extract.py                      per-video extraction script (run on the runner)
videos.json                     video number -> id/title/duration/paper
queue/batch.json                current work queue
```
