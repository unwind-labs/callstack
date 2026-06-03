// Sidechain-duck the background music under the voiceover and mux into the
// final video. Remotion renders VO-only; this adds music in post because
// ducking needs the voice as a sidechain key (Remotion only sums audio).
//
//   npx remotion render Explainer out/_voice.mp4
//   node scripts/mix-music.mjs out/_voice.mp4 out/callstack-explainer.mp4
//
// Music sits ~0.7 in the gaps + intro and ducks to ~0.15 under speech.

import { spawnSync } from "node:child_process";

const IN = process.argv[2] ?? "out/_voice.mp4";
const MUSIC = process.env.MUSIC ?? "public/audio/music.mp3";
const OUT = process.argv[3] ?? "out/callstack-explainer.mp4";

const MUSIC_HI = Number(process.env.MUSIC_HI ?? "0.7"); // un-ducked level (gaps/intro)
// Ducking depth (~0.7 -> ~0.3 under speech) is set by threshold/ratio below.

const probe = spawnSync("ffprobe", [
  "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", IN,
]);
const dur = Number(probe.stdout.toString().trim());
if (!dur) throw new Error(`could not read duration of ${IN}`);
const fadeOut = (dur - 3).toFixed(2);

const filter = [
  `[1:a]volume=${MUSIC_HI},afade=t=in:st=0:d=2,afade=t=out:st=${fadeOut}:d=3[m]`,
  `[m][0:a]sidechaincompress=threshold=0.02:ratio=5:attack=10:release=300:makeup=1:detection=rms[duck]`,
  `[0:a][duck]amix=inputs=2:duration=first:normalize=0[a]`,
].join(";");

const r = spawnSync(
  "ffmpeg",
  [
    "-y", "-i", IN, "-i", MUSIC,
    "-filter_complex", filter,
    "-map", "0:v", "-map", "[a]",
    "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
    OUT,
  ],
  { stdio: "inherit" },
);
process.exit(r.status ?? 1);
