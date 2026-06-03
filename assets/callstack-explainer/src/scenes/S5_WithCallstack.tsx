import React from "react";
import { AbsoluteFill, Sequence, useCurrentFrame } from "remotion";
import { Background, Caption, ramp } from "../ui";
import { COLOR, MONO, SANS } from "../theme";
import { simulate, Op } from "../sim";
import { StackView } from "../StackView";

const OPS: Op[] = [
  { t: 12, kind: "turn", role: "user" },
  { t: 30, kind: "turn", role: "agent" },
  { t: 48, kind: "turn", role: "user" },

  { t: 80, kind: "call", id: "1", label: "task 1" },
  { t: 108, kind: "turn", role: "agent" },
  { t: 128, kind: "turn", role: "user" },

  { t: 156, kind: "call", id: "1.1", label: "task 1.1" },
  { t: 184, kind: "turn", role: "agent" },
  { t: 204, kind: "turn", role: "user" },

  { t: 234, kind: "call", id: "1.1.1", label: "task 1.1.1" },
  { t: 262, kind: "turn", role: "agent" },
  { t: 282, kind: "turn", role: "user" },
  { t: 312, kind: "return", result: "1.1.1 ✓" },

  { t: 356, kind: "call", id: "1.1.2", label: "task 1.1.2" },
  { t: 384, kind: "turn", role: "agent" },
  { t: 404, kind: "turn", role: "user" },

  { t: 434, kind: "call", id: "1.1.2.1", label: "task 1.1.2.1" },
  { t: 462, kind: "turn", role: "agent" },
  { t: 482, kind: "turn", role: "user" },
  { t: 516, kind: "return", result: "1.1.2.1 ✓" },

  { t: 566, kind: "return", result: "1.1.2 ✓" },
  { t: 606, kind: "return", result: "1.1 ✓" },
  { t: 644, kind: "return", result: "task 1 ✓" },
];

const SIM = simulate(OPS);

export const S5_WithCallstack: React.FC = () => {
  const frame = useCurrentFrame();
  const head = ramp(frame, 6, 16);

  return (
    <AbsoluteFill>
      <Background tint="rgba(242,169,59,0.10)" />

      <div
        style={{
          position: "absolute",
          top: 70,
          left: 0,
          right: 0,
          textAlign: "center",
          opacity: head,
        }}
      >
        <div style={{ fontFamily: SANS, fontSize: 38, fontWeight: 700, color: COLOR.text }}>
          With <span style={{ color: COLOR.call }}>callstack</span>
        </div>
        <div style={{ fontFamily: MONO, fontSize: 17, color: COLOR.textDim, marginTop: 6 }}>
          every /call forks a frame — work nests, then unwinds deterministically
        </div>
      </div>

      <StackView sim={SIM} frame={frame} />

      <Sequence from={70} durationInFrames={120} layout="none">
        <Caption total={120} accent={COLOR.call}>
          A <code style={{ fontFamily: MONO, color: COLOR.call }}>/call</code> forks the session —
          the child inherits the full context for free.
        </Caption>
      </Sequence>
      <Sequence from={196} durationInFrames={120} layout="none">
        <Caption total={120} accent={COLOR.call}>
          Copied context stays <span style={{ color: COLOR.textDim }}>dim</span>. The frame only adds
          the new turns it generates.
        </Caption>
      </Sequence>
      <Sequence from={316} durationInFrames={130} layout="none">
        <Caption total={130} accent={COLOR.result}>
          When a call returns, its whole frame collapses into one compact result line.
        </Caption>
      </Sequence>
      <Sequence from={452} durationInFrames={110} layout="none">
        <Caption total={110} accent={COLOR.call}>
          Calls nest as deep as the work needs — each frame stays small and focused.
        </Caption>
      </Sequence>
      <Sequence from={566} durationInFrames={150} layout="none">
        <Caption total={150} accent={COLOR.result}>
          The stack unwinds <span style={{ color: COLOR.ok }}>deterministically</span> — every result
          lands back in the exact caller that asked for it.
        </Caption>
      </Sequence>
    </AbsoluteFill>
  );
};
