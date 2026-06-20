# marketcast-recorder

The Node recorder for **marketcast**. It turns a self-contained Polymarket
dashboard (an HTML file under `dashboards/`) into a clean **1:1 (1080×1080) MP4**
by playing the deck once in a headless Chromium (Playwright), capturing it, and
re-encoding with ffmpeg. Output metadata is stripped so the file carries no
capture fingerprints. Optionally, a royalty-free background-music bed is muxed
underneath the finished video.

It is normally invoked by the Python pipeline (`marketcast.cli`) as a
subprocess: `node record.js <flags> <url>`. It also runs standalone.

## How it works

1. Opens `dashboards/<File>.html` over `file://`, passing the subject through the
   URL query string (`?event=<slug>` or `?addr=<wallet>`, plus optional
   `&hide=<ids>`).
2. If `--copy=<file.json>` is given, the JSON is injected as `window.__aiCopy`
   **before** the page mounts, so the dashboard renders the LLM-written copy.
3. Waits for the deck to mount (`window.__rec.ready`), plays it to completion
   (`window.__rec.finished`), lingers on the closing card, then stops capture.
   A dashboard load error is surfaced via `window.__recError`.
4. Re-encodes to `videos/<name>_<timestamp>.mp4` (libx264, yuv420p, crf 18,
   metadata stripped, faststart).
5. If `music/` contains tracks and `--no-music` wasn't passed, muxes a music bed
   (see `addmusic.js`).

## Prerequisites

- **Node.js** 18+
- `npm install` (installs Playwright)
- `npx playwright install chromium` (downloads the browser)
- **ffmpeg** and **ffprobe** on your `PATH`

## Usage

```bash
# Classic event deck (default dashboard: EventDashboard.html)
node record.js https://polymarket.com/event/some-event-slug
node record.js some-event-slug                 # bare slug also works

# Batch several events
node record.js slug-one slug-two slug-three

# Event grid deck  → EventDashboardGrid.html
node record.js --grid some-event-slug

# Trader grid deck → DashboardGrid.html
node record.js --trader 0xabc...def
node record.js --trader https://polymarket.com/profile/0xabc...def

# Classic trader deck → Dashboard.html
node record.js --trader-classic 0xabc...def

# Hide specific charts/panels by id
node record.js --hide=pnl,positions some-event-slug

# Inject AI-written dashboard copy
node record.js --copy=copy.json some-event-slug

# Skip the music mux step
node record.js --no-music some-event-slug
```

Run with no positional argument to enter an interactive prompt (paste a
URL/wallet per line; empty line quits).

### Dashboard mapping

| Flag               | Dashboard file                       |
| ------------------ | ------------------------------------ |
| _(default)_        | `dashboards/EventDashboard.html`     |
| `--grid`           | `dashboards/EventDashboardGrid.html` |
| `--trader`         | `dashboards/DashboardGrid.html`      |
| `--trader-classic` | `dashboards/Dashboard.html`          |

### `--copy` JSON shape

The file is parsed and exposed verbatim as `window.__aiCopy`:

```json
{
  "hook": "Short attention-grabbing line shown up top.",
  "verdict": "The one-line call / takeaway.",
  "analysis": "A longer paragraph of reasoning the deck renders in the body."
}
```

All three keys are optional; the dashboards read whichever are present.

## Background music (optional)

Drop royalty-free audio files (`.mp3 .wav .m4a .ogg .flac .aac .opus`) into a
`music/` folder next to `record.js`. After each recording, a track is chosen
deterministically per video filename, looped/trimmed to length, loudness-
normalized, faded in/out, and muxed under the video without re-encoding the
picture. The original `*.mp4` is kept; a new `*_music.mp4` is written.

If `music/` is missing or empty, the step is silently skipped.

You can also run it standalone:

```bash
node addmusic.js video.mp4            # → video_music.mp4
node addmusic.js                      # score every un-scored *.mp4 in videos/
```

## Output

Finished videos land in `recorder/videos/`:

- `<slug-or-wallet><tag>_<YYYY-MM-DD-HH-MM>.mp4` — the recording
- `..._music.mp4` — the music-scored copy (when `music/` has tracks)
