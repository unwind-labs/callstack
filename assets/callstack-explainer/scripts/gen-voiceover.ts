// Generate per-scene voiceover via xAI TTS (wss://api.x.ai/v1/tts).
// Run with bun (its WebSocket supports the Authorization header):
//   bun run scripts/gen-voiceover.ts
//
// The narration script is the single source of truth in narration.md — each
// scene is a "**S<id> — ...**" header followed by a "> ..." blockquote. This
// reads XAI_API_KEY from the CareGrid voice spike .env (never logged), streams
// PCM16 @ 24kHz back, transcodes to public/vo/<id>.mp3 via ffmpeg, and writes
// src/vo-manifest.json with each clip's duration (used to size the scenes).

import { readFileSync, writeFileSync, mkdirSync, unlinkSync } from "node:fs";
import { spawnSync } from "node:child_process";

const ENV_PATH =
  process.env.XAI_ENV_PATH ?? "/Users/amolk/work/CareGrid/frontdoor/spikes/voice/.env";

function readKey(): string {
  const env = readFileSync(ENV_PATH, "utf8");
  const m = env.match(/^\s*XAI_API_KEY\s*=\s*(.+?)\s*$/m);
  if (!m) throw new Error(`XAI_API_KEY not found in ${ENV_PATH}`);
  return m[1].replace(/^["']|["']$/g, "").trim();
}

// Parse narration.md into [{ id, text }] in document order.
function loadNarration(path = "narration.md"): { id: string; text: string }[] {
  const lines = readFileSync(path, "utf8").split(/\r?\n/);
  const out: { id: string; text: string }[] = [];
  let id: string | null = null;
  let buf: string[] = [];
  const flush = () => {
    if (id && buf.length) out.push({ id, text: buf.join(" ").trim() });
    id = null;
    buf = [];
  };
  for (const raw of lines) {
    const line = raw.trim();
    const h = line.match(/^\*\*S([0-9]+[a-z]?)\b/i);
    if (h) {
      flush();
      id = "s" + h[1].toLowerCase();
    } else if (line.startsWith(">")) {
      buf.push(line.replace(/^>\s?/, ""));
    } else if (id) {
      flush(); // blank line or notes end the current scene
    }
  }
  flush();
  if (out.length === 0) throw new Error(`no narration found in ${path}`);
  return out;
}

const KEY = readKey();
const VOICE = process.env.XAI_TTS_VOICE ?? "ara";
const SR = 24000;
const OUT_DIR = "public/vo";
const NARRATION = loadNarration();

function synth(text: string): Promise<Buffer> {
  const url = `wss://api.x.ai/v1/tts?language=en&voice=${VOICE}&codec=pcm&sample_rate=${SR}`;
  return new Promise((resolve, reject) => {
    const chunks: Buffer[] = [];
    // bun's WebSocket constructor accepts a headers option.
    const ws = new WebSocket(url, { headers: { Authorization: `Bearer ${KEY}` } } as never);
    const timer = setTimeout(() => {
      try {
        ws.close();
      } catch {
        /* ignore */
      }
      reject(new Error("tts timeout"));
    }, 120000);
    ws.onopen = () => {
      ws.send(JSON.stringify({ type: "text.delta", delta: text }));
      ws.send(JSON.stringify({ type: "text.done" }));
    };
    ws.onmessage = (ev: MessageEvent) => {
      const data = typeof ev.data === "string" ? ev.data : Buffer.from(ev.data).toString("utf8");
      let m: { type: string; delta?: string; message?: string };
      try {
        m = JSON.parse(data);
      } catch {
        return;
      }
      if (m.type === "audio.delta" && m.delta) {
        chunks.push(Buffer.from(m.delta, "base64"));
      } else if (m.type === "audio.done") {
        clearTimeout(timer);
        ws.close();
        resolve(Buffer.concat(chunks));
      } else if (m.type === "error") {
        clearTimeout(timer);
        ws.close();
        reject(new Error(m.message ?? "tts error"));
      }
    };
    ws.onerror = (e: Event) => {
      clearTimeout(timer);
      reject(new Error((e as ErrorEvent).message ?? "ws error"));
    };
  });
}

// Optional ids to regenerate (e.g. `bun run scripts/gen-voiceover.ts s7 s8`);
// unlisted clips keep their existing mp3 + manifest duration. No args = all.
const only = process.argv.slice(2).filter((a) => /^s\d/i.test(a));
const prev: { id: string; seconds: number }[] = (() => {
  try {
    return JSON.parse(readFileSync("src/vo-manifest.json", "utf8"));
  } catch {
    return [];
  }
})();
const prevSec = (id: string): number | null => {
  const m = prev.find((x) => x.id === id);
  return m ? m.seconds : null;
};

mkdirSync(OUT_DIR, { recursive: true });
const manifest: { id: string; seconds: number }[] = [];

for (const { id, text } of NARRATION) {
  const existing = prevSec(id);
  if (only.length > 0 && !only.includes(id) && existing != null) {
    manifest.push({ id, seconds: existing });
    console.log(`keep  ${id} (${existing.toFixed(2)}s)`);
    continue;
  }
  process.stdout.write(`synth ${id} (voice=${VOICE})... `);
  const pcm = await synth(text);
  if (pcm.length === 0) throw new Error(`empty audio for ${id}`);
  const pcmPath = `${OUT_DIR}/${id}.pcm`;
  const mp3Path = `${OUT_DIR}/${id}.mp3`;
  writeFileSync(pcmPath, pcm);
  const r = spawnSync(
    "ffmpeg",
    ["-y", "-f", "s16le", "-ar", String(SR), "-ac", "1", "-i", pcmPath, "-b:a", "160k", mp3Path],
    { stdio: "ignore" },
  );
  unlinkSync(pcmPath);
  if (r.status !== 0) throw new Error(`ffmpeg failed for ${id}`);
  const seconds = Math.round((pcm.length / (SR * 2)) * 1000) / 1000;
  manifest.push({ id, seconds });
  console.log(`${(pcm.length / 1024).toFixed(0)} KB pcm -> mp3, ${seconds.toFixed(2)}s`);
}

writeFileSync("src/vo-manifest.json", JSON.stringify(manifest, null, 2) + "\n");
console.log(`\nwrote src/vo-manifest.json (${manifest.length} clips)`);
