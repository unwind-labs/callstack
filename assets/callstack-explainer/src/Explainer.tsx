import React from "react";
import { AbsoluteFill, Audio, Sequence, staticFile, useCurrentFrame } from "remotion";
import { Background, fadeInOut } from "./ui";
import { COLOR, FPS } from "./theme";
import voManifest from "./vo-manifest.json";
import { S1_Problem } from "./scenes/S1_Problem";
import { S2_Solution } from "./scenes/S2_Solution";
import { S3_Results } from "./scenes/S3_Results";
import { S5_WithCallstack } from "./scenes/S5_WithCallstack";
import { S5b_Parallel } from "./scenes/S5b_Parallel";
import { S6_ForkFresh } from "./scenes/S6_ForkFresh";
import { S7_DeepInteraction } from "./scenes/S7_DeepInteraction";
import { S7B_Why } from "./scenes/S7B_Why";
import { S8_Install } from "./scenes/S8_Install";

const GAP = 15; // brief backdrop pause between scenes (~0.5s)
const AUDIO_LEAD = 8; // frames of silence before narration starts
const AUDIO_TAIL = 20; // min frames of hold after narration ends
const LEAD_IN = 90; // music-only intro (~3s) before the first scene
// Music is mixed in post (sidechain ducking) — see scripts/mix-music.mjs.

const voSeconds = (id: string): number => {
  const f = (voManifest as { id: string; seconds: number }[]).find((v) => v.id === id);
  return f ? f.seconds : 0;
};

// Each scene runs for at least its design length, and always long enough to
// hold its full voiceover clip plus a short lead/tail.
const sceneDur = (designDur: number, id: string): number =>
  Math.max(designDur, AUDIO_LEAD + Math.ceil(voSeconds(id) * FPS) + AUDIO_TAIL);

const SCENES: { id: string; Comp: React.FC; design: number }[] = [
  { id: "s1", Comp: S1_Problem, design: 200 },
  { id: "s2", Comp: S2_Solution, design: 165 },
  { id: "s3", Comp: S3_Results, design: 270 },
  { id: "s5", Comp: S5_WithCallstack, design: 730 },
  { id: "s5b", Comp: S5b_Parallel, design: 430 },
  { id: "s6", Comp: S6_ForkFresh, design: 165 },
  { id: "s7", Comp: S7_DeepInteraction, design: 640 },
  { id: "s7b", Comp: S7B_Why, design: 200 },
  { id: "s8", Comp: S8_Install, design: 210 },
].map((s) => ({ ...s, design: sceneDur(s.design, s.id) }));

// Cumulative starts with a small crossfade overlap between scenes.
const STARTS: number[] = [];
let acc = 0;
for (let i = 0; i < SCENES.length; i++) {
  STARTS.push(acc);
  acc += SCENES[i].design + GAP;
}
export const EXPLAINER_DURATION = LEAD_IN + acc - GAP;

const SceneWrap: React.FC<{ dur: number; children: React.ReactNode }> = ({ dur, children }) => {
  const frame = useCurrentFrame();
  return <AbsoluteFill style={{ opacity: fadeInOut(frame, dur, 12, 14) }}>{children}</AbsoluteFill>;
};

export const Explainer: React.FC = () => {
  return (
    <AbsoluteFill style={{ backgroundColor: COLOR.bg0 }}>
      {/* root backdrop — visible during the music-only lead-in */}
      <Background />
      {SCENES.map(({ id, Comp, design }, i) => (
        <Sequence key={id} from={LEAD_IN + STARTS[i]} durationInFrames={design} premountFor={30}>
          <Sequence from={AUDIO_LEAD} layout="none">
            <Audio src={staticFile(`vo/${id}.mp3`)} />
          </Sequence>
          <SceneWrap dur={design}>
            <Comp />
          </SceneWrap>
        </Sequence>
      ))}
    </AbsoluteFill>
  );
};
