import React from "react";
import { AbsoluteFill, useCurrentFrame, useVideoConfig, interpolate, Easing } from "remotion";
import { Background, ramp, StackPointer, TurnBlock } from "../ui";
import { COLOR, MONO, SANS, WIDTH, FPS } from "../theme";
import voManifest from "../vo-manifest.json";

// Beats are derived from the S2 clip length so they track the narration:
//   0.28  heading rises as "One command…" begins
//   0.33  the /call mechanism runs (fork → run → return)
//   0.59  "if we ask the model to keep track of the stack, it is not reliable" → jitter
//   0.70  "but with a deterministic call stack…" → resolves to green
const CLIP =
  ((voManifest as { id: string; seconds: number }[]).find((v) => v.id === "s2")?.seconds ?? 26) *
  FPS;
const beat = (p: number): number => Math.round(8 + p * CLIP);
const HEAD_RISE = beat(0.28);
const MECH_START = beat(0.33);
const JITTER_IN = beat(0.59);
const FLIP_START = beat(0.7);
const EASE_IN = Easing.in(Easing.cubic);

const PARENT_X = 524;
const PARENT_W = 384;
const CHILD_X = 1012;
const CHILD_W = 372;
const PANEL_TOP = 452;
const HEADER_H = 44;

const FrameBox: React.FC<{
  x: number;
  w: number;
  top: number;
  accent: string;
  tag: string;
  label: string;
  appear: number;
  exit?: number;
  children: React.ReactNode;
}> = ({ x, w, top, accent, tag, label, appear, exit = 0, children }) => (
  <div
    style={{
      position: "absolute",
      left: x,
      top,
      width: w,
      opacity: appear * (1 - exit),
      transform: `translateY(${interpolate(appear, [0, 1], [22, 0]) - exit * 40}px)`,
    }}
  >
    <div
      style={{
        height: HEADER_H,
        borderRadius: "12px 12px 0 0",
        background: `${accent}1f`,
        border: `1px solid ${accent}55`,
        display: "flex",
        alignItems: "center",
        gap: 9,
        padding: "0 14px",
        boxSizing: "border-box",
      }}
    >
      <span
        style={{
          fontFamily: MONO,
          fontSize: 12,
          color: accent,
          background: `${accent}22`,
          borderRadius: 5,
          padding: "2px 7px",
          fontWeight: 700,
        }}
      >
        {tag}
      </span>
      <span style={{ fontFamily: SANS, fontSize: 17, fontWeight: 600, color: COLOR.text }}>
        {label}
      </span>
    </div>
    <div
      style={{
        background: COLOR.panel,
        border: `1px solid ${accent}44`,
        borderTop: "none",
        borderRadius: "0 0 12px 12px",
        padding: 14,
        boxSizing: "border-box",
        display: "flex",
        flexDirection: "column",
        gap: 9,
      }}
    >
      {children}
    </div>
  </div>
);

const Pill: React.FC<{ appear: number; color: string; soft: string; children: React.ReactNode }> = ({
  appear,
  color,
  soft,
  children,
}) => (
  <div style={{ height: 32 * appear, overflow: "hidden", opacity: appear }}>
    <div
      style={{
        height: 32,
        borderRadius: 9,
        background: soft,
        border: `1px solid ${color}66`,
        borderLeft: `3px solid ${color}`,
        display: "flex",
        alignItems: "center",
        gap: 8,
        padding: "0 12px",
        boxSizing: "border-box",
        fontFamily: MONO,
        fontSize: 14,
        color,
      }}
    >
      {children}
    </div>
  </div>
);

// One /call: the session forks, the child runs in its own clean frame, and at
// ~48s its single compact result returns and appends to the parent.
const CallMechanism: React.FC<{ frame: number }> = ({ frame }) => {
  const pAppear = ramp(frame, MECH_START, 18);
  const callLine = ramp(frame, MECH_START + 28, 14);
  const fork = ramp(frame, MECH_START + 72, 18);
  const run1 = ramp(frame, MECH_START + 116, 14);
  const run2 = ramp(frame, MECH_START + 156, 14);
  const childExit = interpolate(frame, [MECH_START + 192, MECH_START + 212], [0, 1], {
    easing: EASE_IN,
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });
  const resultPill = ramp(frame, MECH_START + 210, 14); // lands ~528 (48s)
  // a pulse travelling the connector as the result returns
  const retPulse = interpolate(frame, [MECH_START + 196, MECH_START + 214], [0, 1], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });

  const t = frame - MECH_START;
  const stage = t < 116 ? "① fork the session" : t < 196 ? "② run in a clean frame" : "③ return one compact result";

  const oy = PANEL_TOP + HEADER_H + 14 + 70; // fork/return origin y in parent
  const cy = PANEL_TOP + HEADER_H / 2;
  const px = (v: number): number => PARENT_X + PARENT_W + (CHILD_X - (PARENT_X + PARENT_W)) * v;
  const py = (v: number): number => oy + (cy - oy) * v;
  return (
    <>
      {/* fork connector */}
      <svg width={WIDTH} height={1080} style={{ position: "absolute", inset: 0, pointerEvents: "none" }}>
        <g opacity={fork * (1 - childExit) * 0.85}>
          <path
            d={`M ${PARENT_X + PARENT_W} ${oy} C ${PARENT_X + PARENT_W + 50} ${oy}, ${CHILD_X - 50} ${cy}, ${CHILD_X} ${cy}`}
            stroke={COLOR.call}
            strokeWidth={2}
            fill="none"
            strokeDasharray="2 5"
            strokeLinecap="round"
          />
          <circle cx={PARENT_X + PARENT_W} cy={oy} r={3} fill={COLOR.call} />
          <circle cx={CHILD_X} cy={cy} r={3.5} fill={COLOR.call} />
        </g>
        {/* result returning to the parent */}
        {retPulse > 0 && retPulse < 1 ? (
          <circle cx={px(1 - retPulse)} cy={py(1 - retPulse)} r={6} fill={COLOR.result} />
        ) : null}
      </svg>

      {/* parent session */}
      <FrameBox
        x={PARENT_X}
        w={PARENT_W}
        top={PANEL_TOP}
        accent={COLOR.user}
        tag="ROOT"
        label="main session"
        appear={pAppear}
      >
        <div style={{ height: 30 * pAppear, overflow: "hidden" }}>
          <TurnBlock role="user" height={30} width={PARENT_W - 28} appear={pAppear} />
        </div>
        <div style={{ height: 54 * pAppear, overflow: "hidden" }}>
          <TurnBlock role="agent" height={54} width={PARENT_W - 28} appear={pAppear} bullets={2} seed={2} />
        </div>
        <div style={{ height: 32 * callLine, overflow: "hidden", opacity: callLine }}>
          <div
            style={{
              height: 32,
              borderRadius: 9,
              background: COLOR.callSoft,
              border: `1px solid ${COLOR.call}66`,
              borderLeft: `3px solid ${COLOR.call}`,
              display: "flex",
              alignItems: "center",
              gap: 8,
              padding: "0 12px",
              boxSizing: "border-box",
              fontFamily: MONO,
              fontSize: 14,
              color: COLOR.call,
            }}
          >
            <span style={{ fontWeight: 700 }}>/call</span>
            <span style={{ color: COLOR.text, opacity: 0.85 }}>implement auth</span>
          </div>
        </div>
        <Pill appear={resultPill} color={COLOR.result} soft={COLOR.resultSoft}>
          <span style={{ fontWeight: 700 }}>↩</span>
          <span style={{ color: COLOR.text, opacity: 0.85 }}>auth ready ✓</span>
        </Pill>
      </FrameBox>

      {/* forked child */}
      <FrameBox
        x={CHILD_X}
        w={CHILD_W}
        top={PANEL_TOP}
        accent={COLOR.call}
        tag="/call"
        label="implement auth"
        appear={fork}
        exit={childExit}
      >
        <div
          style={{
            border: "1px dashed rgba(255,255,255,0.12)",
            borderRadius: 9,
            padding: "8px 10px",
            display: "flex",
            flexDirection: "column",
            gap: 5,
            background: "rgba(255,255,255,0.015)",
          }}
        >
          <div style={{ fontFamily: MONO, fontSize: 12, color: COLOR.textFaint }}>
            ⤴ inherited context · full parent transcript
          </div>
          {[82, 60, 72].map((w, k) => (
            <div
              key={k}
              style={{
                height: 5,
                borderRadius: 5,
                background: k % 2 === 0 ? "rgba(77,141,246,0.28)" : "rgba(45,212,167,0.28)",
                width: `${w}%`,
              }}
            />
          ))}
        </div>
        <div style={{ height: 70 * run1, overflow: "hidden" }}>
          <TurnBlock role="agent" height={70} width={CHILD_W - 28} appear={run1} bullets={3} seed={5} />
        </div>
        <div style={{ height: 34 * run2, overflow: "hidden" }}>
          <TurnBlock role="user" height={34} width={CHILD_W - 28} appear={run2} />
        </div>
      </FrameBox>

      {/* stage label */}
      <div
        style={{
          position: "absolute",
          top: PANEL_TOP + 300,
          left: 0,
          right: 0,
          textAlign: "center",
          fontFamily: MONO,
          fontSize: 20,
          fontWeight: 700,
          color: COLOR.call,
          opacity: pAppear,
        }}
      >
        {stage}
      </div>
    </>
  );
};

export const S2_Solution: React.FC = () => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const mark = ramp(frame, 0.2 * fps, 0.6 * fps);
  const tag = ramp(frame, 0.6 * fps, 0.5 * fps);
  const cmd = ramp(frame, 1.0 * fps, 0.5 * fps);

  // Heading rises from centered to top at 39s.
  const rise = ramp(frame, HEAD_RISE, 30);
  const headTop = interpolate(rise, [0, 1], [410, 92]);

  // Mechanism runs until the jitter panel takes over; jitter holds, then flips.
  const mechOp = ramp(frame, MECH_START, 20) * (1 - ramp(frame, JITTER_IN - 14, 16));
  const spOp = ramp(frame, JITTER_IN, 20);
  const flip = ramp(frame, FLIP_START, 24);

  return (
    <AbsoluteFill>
      <Background tint="rgba(242,169,59,0.12)" />

      {/* heading — centered, then rises */}
      <div style={{ position: "absolute", top: headTop, left: 0, right: 0, textAlign: "center" }}>
        <div
          style={{ opacity: mark, marginBottom: 18, display: "flex", gap: 0, justifyContent: "center" }}
        >
          {[0, 1, 2].map((i) => (
            <div
              key={i}
              style={{
                width: 46,
                height: 46,
                marginLeft: i === 0 ? 0 : -8,
                marginTop: i * 10,
                borderRadius: 9,
                background: `${COLOR.call}${["1f", "2c", "3a"][i]}`,
                border: `1.5px solid ${COLOR.call}${["55", "77", "99"][i]}`,
                transform: `translateY(${interpolate(mark, [0, 1], [16 + i * 6, 0])}px)`,
              }}
            />
          ))}
        </div>
        <div
          style={{
            fontFamily: MONO,
            fontSize: 64,
            fontWeight: 700,
            letterSpacing: -2,
            color: COLOR.text,
            opacity: mark,
            transform: `translateY(${interpolate(mark, [0, 1], [14, 0])}px)`,
          }}
        >
          <span style={{ color: COLOR.call }}>call</span>stack
        </div>
        <div
          style={{
            fontFamily: SANS,
            fontSize: 30,
            fontWeight: 600,
            color: COLOR.text,
            marginTop: 14,
            opacity: tag,
          }}
        >
          A deterministic call stack for agents.
        </div>
        <div
          style={{
            marginTop: 20,
            opacity: cmd,
            display: "flex",
            justifyContent: "center",
            alignItems: "center",
            gap: 12,
            fontFamily: MONO,
            fontSize: 18,
            color: COLOR.textDim,
          }}
        >
          <span
            style={{
              color: COLOR.call,
              background: COLOR.callSoft,
              border: `1px solid ${COLOR.call}55`,
              borderRadius: 8,
              padding: "5px 12px",
              fontWeight: 700,
            }}
          >
            /call
          </span>
          <span>
            fork the session <span style={{ color: COLOR.textFaint }}>·</span> run in a clean frame{" "}
            <span style={{ color: COLOR.textFaint }}>·</span> return a compact result
          </span>
        </div>
      </div>

      {/* the /call mechanism */}
      {mechOp > 0.01 ? (
        <AbsoluteFill style={{ opacity: mechOp }}>
          <CallMechanism frame={frame} />
        </AbsoluteFill>
      ) : null}

      {/* model jitter → harness (no-jitter) */}
      {spOp > 0.01 ? (
        <div
          style={{
            position: "absolute",
            top: 470,
            left: 0,
            right: 0,
            display: "flex",
            justifyContent: "center",
            opacity: spOp,
          }}
        >
          <div style={{ transform: "scale(1.34)", transformOrigin: "top center" }}>
            <StackPointer frame={frame} flip={flip} />
          </div>
        </div>
      ) : null}
    </AbsoluteFill>
  );
};
