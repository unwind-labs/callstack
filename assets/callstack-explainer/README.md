# callstack — explainer video

A ~140-second animated explainer for **callstack**, built with [Remotion](https://remotion.dev).
One composition, `Explainer` (1920×1080, 30fps), assembled from nine scenes.

## Preview / render

```bash
npm install          # once
npm run dev          # Remotion Studio (live preview / scrubbing)
npm run lint         # eslint + tsc

# Full build = render the VO-only video, then mix in the ducked music bed:
npx remotion render Explainer out/_voice.mp4
node scripts/mix-music.mjs out/_voice.mp4 out/callstack-explainer.mp4
```

Remotion renders the visuals + voiceover; the music is added in post because it
sidechain-ducks under the voice (see [Background music](#background-music)).

A QA helper renders stills without re-bundling each time:

```bash
node scripts/stills.mjs 90 487 1454   # frame numbers -> /tmp/exp_<frame>.png
```

## Scenes (`src/scenes/`)

| # | File | Beat |
|---|------|------|
| 1 | `S1_Problem` | Complex nested workflows overflow a single ReAct loop. |
| 2 | `S2_Solution` | callstack — a deterministic call stack for agents. |
| 3 | `S3_Results` | Benchmark: 500+ nodes, 7 levels. 100% with, fails by depth 5 without. |
| 4 | `S4_WithoutCallstack` | One linear session keeps nesting until the agent loses the thread. |
| 5 | `S5_WithCallstack` | Each `/call` forks a frame; copied context dims; the stack unwinds. |
| 6 | `S6_ForkFresh` | `context="fork"` vs `context="fresh"`. |
| 7 | `S7_DeepInteraction` | A frame 4 levels deep yields straight to the user (customer-support MFA). |
| 8 | `S8_Install` | Install commands + outro. |

## Voiceover

Narration is generated with **xAI TTS** (`wss://api.x.ai/v1/tts`, voice `ara`, PCM16 @ 24kHz) and muxed in at render time. `narration.md` may use xAI markers — `<emphasis>…</emphasis>` and `[pause]` — which the model honors natively (sent through as-is).

```bash
bun run scripts/gen-voiceover.ts   # -> public/vo/s1..s8.mp3 + src/vo-manifest.json
```

- The script reads `XAI_API_KEY` from the CareGrid voice spike `.env` (path overridable via `XAI_ENV_PATH`); the key is never logged. Voice is overridable via `XAI_TTS_VOICE` (`eve | ara | rex | sal | leo`).
- Edit the narration text in `scripts/gen-voiceover.ts` (`NARRATION`), re-run, and re-render.
- `src/vo-manifest.json` records each clip's duration; `Explainer.tsx` sizes every scene to hold its full clip (`max(design, lead + clip + tail)`), so changing narration length automatically re-times the video. On-screen captions remain as subtitles.

## Background music

The bed at `public/audio/music.mp3` (a ~146 s segment trimmed from the source track) is **sidechain-ducked** under the voice by `scripts/mix-music.mjs`: it sits at ~0.7 in the gaps and the 3 s intro, and dips to ~0.15 whenever the narration is speaking. There's a ~3 s music-only lead-in (Remotion `LEAD_IN`) before scene 1.

```bash
# trim a fresh bed from a source track:
ffmpeg -y -ss 0 -t 146 -i <source>.mp3 -c:a libmp3lame -b:a 160k public/audio/music.mp3
# then re-run the build (render VO-only + mix). Tune loudness/duck:
MUSIC_HI=0.7 node scripts/mix-music.mjs out/_voice.mp4 out/callstack-explainer.mp4
```

`MUSIC_HI` sets the un-ducked level; the duck depth is the `threshold`/`ratio` in the `sidechaincompress` filter. **Confirm the music license before publishing.**

## Architecture

- `src/theme.ts` — colors, fonts, block/frame geometry.
- `src/ui.tsx` — shared primitives: `Background`, `Caption`, `TurnBlock`, `TitleBlock`, `Pill`, and the `ramp` / `fadeInOut` timing helpers.
- `src/sim.ts` — a pure simulator: replays a list of timed `turn` / `call` / `return` ops into per-frame call-stack geometry, plus a smooth `cameraDepth` follower.
- `src/StackView.tsx` — renders a `Sim` as cascading call frames (inherited-context band, animated turns, return pills, connectors, breadcrumb). Shared by scenes 5 and 7's model.
- `src/Explainer.tsx` — sequences the scenes with short crossfades; exports `EXPLAINER_DURATION`.
- `src/Root.tsx` — registers the composition.

## Tweaking

- **Timing / order:** scene durations and the crossfade `OVERLAP` live in `src/Explainer.tsx`.
- **The call-stack animation:** edit the `OPS` timeline in `src/scenes/S5_WithCallstack.tsx` — the `simulate()` reducer turns it into geometry automatically.
- **Colors / fonts:** `src/theme.ts`. Fonts use system stacks (SF Mono / system-ui); swap in `@remotion/google-fonts` for fixed cross-machine rendering.
- **Captions:** each animation scene drives its narration via `<Sequence>` + `<Caption>`; no audio track is included.
