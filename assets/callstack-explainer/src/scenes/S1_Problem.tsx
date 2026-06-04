import React from "react";
import { AbsoluteFill, useCurrentFrame, interpolate } from "remotion";
import { Background, ramp } from "../ui";
import { COLOR, MONO, SANS } from "../theme";

// Scene-local frames @30fps (global ≈ local/30 + 3s):
const RISE = 360; //   15s — the 3 text lines rise from center to top
const BOXES = 390; //  16s — the 3 fix boxes appear
const X0 = 452; //   18.07s — X over compaction
const X1 = 540; //     21s — X over subagents
const X2 = 630; //     24s — X over bigger context window

// The three "usual fixes" the narration names — each gets a big X as it fails.
const FIXES = [
  { name: "Compaction", fail: "lossy — drops detail", at: X0 },
  { name: "Subagents", fail: "can't nest", at: X1 },
  { name: "Bigger context window", fail: "just fills & rots", at: X2 },
];

const BOX_W = 360;
const BOX_H = 168;

const Fix: React.FC<{ name: string; fail: string; appear: number; x: number }> = ({
  name,
  fail,
  appear,
  x,
}) => {
  const failed = x > 0.5;
  const d1 = Math.max(0, Math.min(1, x * 2)); // first diagonal draws over x 0–0.5
  const d2 = Math.max(0, Math.min(1, x * 2 - 1)); // second over 0.5–1
  return (
    <div
      style={{
        width: BOX_W,
        opacity: appear,
        transform: `translateY(${interpolate(appear, [0, 1], [24, 0])}px)`,
      }}
    >
      <div
        style={{
          position: "relative",
          background: COLOR.panel,
          border: `1px solid ${failed ? COLOR.danger + "88" : COLOR.panelEdge}`,
          borderRadius: 14,
          padding: "26px 24px",
          height: BOX_H,
          boxSizing: "border-box",
          display: "flex",
          flexDirection: "column",
          justifyContent: "center",
          gap: 14,
          overflow: "hidden",
        }}
      >
        <div
          style={{
            fontFamily: SANS,
            fontSize: 30,
            fontWeight: 700,
            color: COLOR.text,
            opacity: 1 - x * 0.4,
          }}
        >
          {name}
        </div>
        <div style={{ fontFamily: MONO, fontSize: 18, color: COLOR.textDim, opacity: 1 - x * 0.4 }}>
          {fail}
        </div>
        {/* big X drawn over the box as it fails */}
        <svg
          width={BOX_W}
          height={BOX_H}
          style={{ position: "absolute", inset: 0, pointerEvents: "none" }}
        >
          <line
            x1={16}
            y1={16}
            x2={BOX_W - 16}
            y2={BOX_H - 16}
            stroke={COLOR.danger}
            strokeWidth={7}
            strokeLinecap="round"
            pathLength={1}
            strokeDasharray={1}
            strokeDashoffset={1 - d1}
          />
          <line
            x1={BOX_W - 16}
            y1={16}
            x2={16}
            y2={BOX_H - 16}
            stroke={COLOR.danger}
            strokeWidth={7}
            strokeLinecap="round"
            pathLength={1}
            strokeDasharray={1}
            strokeDashoffset={1 - d2}
          />
        </svg>
      </div>
    </div>
  );
};

export const S1_Problem: React.FC = () => {
  const frame = useCurrentFrame();
  const kick = ramp(frame, 6, 12);
  const title = ramp(frame, 14, 18);
  const sub = ramp(frame, 26, 18);

  // The 3 lines start vertically centered, then rise to the top at 15s.
  const rise = ramp(frame, RISE, 30);
  const textTop = interpolate(rise, [0, 1], [420, 118]);
  const boxesAppear = ramp(frame, BOXES, 20);

  return (
    <AbsoluteFill>
      <Background tint="rgba(242,84,91,0.10)" />

      {/* title block — centered, then rises */}
      <div style={{ position: "absolute", top: textTop, left: 0, right: 0, textAlign: "center", padding: "0 140px" }}>
        <div
          style={{
            fontFamily: MONO,
            fontSize: 21,
            letterSpacing: 4,
            textTransform: "uppercase",
            color: COLOR.danger,
            opacity: kick,
            marginBottom: 22,
          }}
        >
          The problem
        </div>
        <div
          style={{
            fontFamily: SANS,
            fontSize: 64,
            fontWeight: 700,
            lineHeight: 1.08,
            letterSpacing: -1.5,
            color: COLOR.text,
            opacity: title,
          }}
        >
          Deep workflows break agents.
        </div>
        <div
          style={{
            fontFamily: SANS,
            fontSize: 27,
            fontWeight: 400,
            lineHeight: 1.4,
            color: COLOR.textDim,
            opacity: sub,
            marginTop: 22,
            maxWidth: 1180,
            marginLeft: "auto",
            marginRight: "auto",
          }}
        >
          LLMs track control flow probabilistically — the deeper the nesting, the more likely a step
          is skipped, run out of order, or its place lost. And the usual fixes don&rsquo;t help:
        </div>
      </div>

      {/* the three failing fixes */}
      <div
        style={{
          position: "absolute",
          top: 506,
          left: 0,
          right: 0,
          display: "flex",
          justifyContent: "center",
          gap: 44,
          opacity: boxesAppear,
        }}
      >
        {FIXES.map((f) => (
          <Fix
            key={f.name}
            name={f.name}
            fail={f.fail}
            appear={boxesAppear}
            x={ramp(frame, f.at, 16)}
          />
        ))}
      </div>
    </AbsoluteFill>
  );
};
