/* ─────────────────────────────────────────────────────────────────────────
   addmusic.js — mux a random track from ./music under a recorded video.

   Usage:
     node addmusic.js <video.mp4>        → write <video>_music.mp4 next to it
     node addmusic.js <v1> <v2> ...       → batch
     node addmusic.js                     → process every *.mp4 in ./videos
                                            that doesn't already have _music

   Picks a (deterministic-per-filename) file from ./music
   (.mp3/.wav/.m4a/.ogg/.flac/.aac/.opus), loops it if shorter than the video,
   trims it to video length, applies loudnorm + 0.4s fade-in / 1.2s fade-out,
   then muxes the audio onto the video without re-encoding the picture.
   The source video is left untouched.

   The music folder is optional: it defaults to recorder/music and the whole
   step is skipped when that folder is empty (record.js checks listTracks()).

   Requires ffmpeg / ffprobe on PATH. Called automatically by record.js after
   each recording when ./music contains at least one track (skip --no-music).

   Public API (consumed by record.js): listTracks(), mux().
   ───────────────────────────────────────────────────────────────────────── */
'use strict';

const path = require('path');
const fs   = require('fs');
const { spawnSync } = require('child_process');

const MUSIC_DIR = path.join(__dirname, 'music');   // → recorder/music (optional)
const VIDEO_DIR = path.join(__dirname, 'videos');
const EXTS = new Set(['.mp3', '.wav', '.m4a', '.ogg', '.flac', '.aac', '.opus']);
const FADE_IN  = 0.4;
const FADE_OUT = 1.2;
const MUSIC_DB = -16;   // target LUFS-ish loudness for the music bed

function listTracks() {
  if (!fs.existsSync(MUSIC_DIR)) return [];
  return fs.readdirSync(MUSIC_DIR)
    .filter(f => EXTS.has(path.extname(f).toLowerCase()))
    .map(f => path.join(MUSIC_DIR, f));
}

function pickTrack(seed) {
  const list = listTracks();
  if (!list.length) return null;
  // mildly deterministic per-video: hash the filename so the same recording
  // doesn't shuffle between re-runs, but different videos get different tracks
  let h = 0;
  for (const c of (seed || '')) h = (h * 31 + c.charCodeAt(0)) | 0;
  return list[Math.abs(h) % list.length];
}

function probeDuration(file) {
  const r = spawnSync('ffprobe', [
    '-v', 'error', '-show_entries', 'format=duration',
    '-of', 'default=nw=1:nk=1', file,
  ], { encoding: 'utf8' });
  if (r.status !== 0) return null;
  const v = parseFloat((r.stdout || '').trim());
  return Number.isFinite(v) ? v : null;
}

function mux(videoPath, opts = {}) {
  if (!fs.existsSync(videoPath)) {
    console.error(`  ✗ not found: ${videoPath}`); return false;
  }
  const track = pickTrack(path.basename(videoPath));
  if (!track) {
    console.error('  ⚠ ./music is empty — drop royalty-free tracks in there.');
    return false;
  }

  const vDur = probeDuration(videoPath);
  if (!vDur) { console.error('  ✗ could not probe video length'); return false; }

  const fadeOutStart = Math.max(0, vDur - FADE_OUT);
  const ext = path.extname(videoPath);
  const out = opts.out || videoPath.replace(new RegExp(ext + '$'), '_music' + ext);

  console.log(`  · music: ${path.basename(track)}`);

  // -stream_loop -1 + -t <vDur> on the audio: loop the track and stop at video length.
  // afade in/out + loudnorm shape it into a non-intrusive bed.
  const args = [
    '-y',
    '-i', videoPath,
    '-stream_loop', '-1', '-i', track,
    '-map', '0:v:0', '-map', '1:a:0',
    '-c:v', 'copy',
    '-af',
      `aresample=48000,loudnorm=I=${MUSIC_DB}:TP=-1.5:LRA=11,` +
      `afade=t=in:st=0:d=${FADE_IN},` +
      `afade=t=out:st=${fadeOutStart.toFixed(3)}:d=${FADE_OUT}`,
    '-c:a', 'aac', '-b:a', '192k',
    // keep the final _music.mp4 metadata-clean: no inherited tags, no
    // "encoder=Lavf…" muxer tag, no "Lavc…" aac encoder tag
    '-map_metadata', '-1',
    '-fflags', '+bitexact', '-flags:a', '+bitexact',
    '-shortest',
    '-movflags', '+faststart',
    out,
  ];
  const ff = spawnSync('ffmpeg', args, { stdio: ['ignore', 'ignore', 'ignore'] });
  if (ff.status !== 0) { console.error('  ✗ ffmpeg failed'); return false; }
  console.log(`  ✓ scored → ${out}`);
  return true;
}

function listUnscored() {
  if (!fs.existsSync(VIDEO_DIR)) return [];
  return fs.readdirSync(VIDEO_DIR)
    .filter(f => f.toLowerCase().endsWith('.mp4') && !/_music\.mp4$/i.test(f))
    .map(f => path.join(VIDEO_DIR, f));
}

function main() {
  const args = process.argv.slice(2).filter(a => !a.startsWith('-'));
  const targets = args.length ? args : listUnscored();
  if (!targets.length) {
    console.error('nothing to score — pass a video path or drop .mp4 files in ./videos');
    process.exit(1);
  }
  if (!listTracks().length) {
    console.error(`✗ ./music is empty.  Drop one or more audio files into:\n  ${MUSIC_DIR}`);
    process.exit(1);
  }
  for (const v of targets) {
    console.log(`▶ ${path.basename(v)}`);
    mux(v);
    console.log('');
  }
}

if (require.main === module) main();

module.exports = { mux, listTracks, pickTrack };
