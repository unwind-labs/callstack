import React from "react";
import { AbsoluteFill, useCurrentFrame, useVideoConfig, interpolate, Easing } from "remotion";
import { Background, ramp } from "../ui";
import { COLOR, MONO, SANS } from "../theme";

const POP = Easing.bezier(0.34, 1.56, 0.64, 1);

// Columns: subagents, graph-based agents, callstack.
const COLS = ["subagents", "graph-based agents", "callstack"];
const ROWS: { cap: string; vals: [boolean, boolean, boolean] }[] = [
  { cap: "Dynamic — decided at runtime", vals: [true, false, true] },
  { cap: "Arbitrarily deep nesting", vals: [false, true, true] },
  { cap: "Interactive at any level", vals: [false, false, true] },
];

const CAP_W = 540;
const COL_W = 286;
const CARD_W = CAP_W + COLS.length * COL_W;
const HEADER_H = 72;
const ROW_H = 86;
const CALLSTACK_COL = 2;

const Mark: React.FC<{ ok: boolean; appear: number }> = ({ ok, appear }) => {
  const s = interpolate(appear, [0, 1], [0.3, 1], {
    easing: POP,
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });
  const color = ok ? COLOR.ok : COLOR.danger;
  return (
    <div
      style={{
        width: 38,
        height: 38,
        borderRadius: 38,
        background: `${color}22`,
        border: `2px solid ${color}`,
        color,
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        fontFamily: SANS,
        fontSize: 22,
        fontWeight: 800,
        transform: `scale(${s})`,
        opacity: appear,
      }}
    >
      {ok ? "✓" : "✕"}
    </div>
  );
};

export const S7B_Why: React.FC = () => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const title = ramp(frame, 0.2 * fps, 0.5 * fps);
  const head = ramp(frame, 0.6 * fps, 0.4 * fps);
  const band = ramp(frame, 0.8 * fps, 0.5 * fps);
  const footer = ramp(frame, 0.6 * fps + ROWS.length * 9 + 16, 0.5 * fps);

  const cardLeft = (1920 - CARD_W) / 2;
  const cardTop = 300;

  return (
    <AbsoluteFill>
      <Background tint="rgba(45,212,167,0.10)" />

      <div
        style={{
          position: "absolute",
          top: 190,
          left: 0,
          right: 0,
          textAlign: "center",
          opacity: title,
          transform: `translateY(${interpolate(title, [0, 1], [14, 0])}px)`,
        }}
      >
        <div style={{ fontFamily: SANS, fontSize: 56, fontWeight: 700, color: COLOR.text, letterSpacing: -1 }}>
          Why <span style={{ color: COLOR.call }}>callstack</span>
        </div>
      </div>

      <div style={{ position: "absolute", left: cardLeft, top: cardTop, width: CARD_W }}>
        {/* highlighted callstack column band */}
        <div
          style={{
            position: "absolute",
            left: CAP_W + CALLSTACK_COL * COL_W + 8,
            top: -12,
            width: COL_W - 16,
            height: (HEADER_H + ROWS.length * ROW_H + 22) * band,
            background: "rgba(45,212,167,0.09)",
            border: `1px solid ${COLOR.ok}55`,
            borderRadius: 14,
          }}
        />

        {/* header row */}
        <div style={{ display: "flex", height: HEADER_H, alignItems: "center", opacity: head }}>
          <div style={{ width: CAP_W }} />
          {COLS.map((c, i) => {
            const isCs = i === CALLSTACK_COL;
            return (
              <div
                key={c}
                style={{
                  width: COL_W,
                  textAlign: "center",
                  fontFamily: MONO,
                  fontSize: isCs ? 23 : 18,
                  fontWeight: isCs ? 700 : 500,
                  color: isCs ? COLOR.ok : COLOR.textDim,
                }}
              >
                {c}
              </div>
            );
          })}
        </div>

        {/* rows */}
        {ROWS.map((row, ri) => {
          const ap = ramp(frame, 0.6 * fps + ri * 9, 12);
          return (
            <div
              key={row.cap}
              style={{
                display: "flex",
                alignItems: "center",
                height: ROW_H,
                borderTop: `1px solid ${COLOR.panelEdge}`,
                opacity: ap,
              }}
            >
              <div
                style={{
                  width: CAP_W,
                  fontFamily: SANS,
                  fontSize: 26,
                  fontWeight: 600,
                  color: COLOR.text,
                  paddingRight: 24,
                }}
              >
                {row.cap}
              </div>
              {row.vals.map((ok, ci) => (
                <div key={ci} style={{ width: COL_W, display: "flex", justifyContent: "center" }}>
                  <Mark ok={ok} appear={ap} />
                </div>
              ))}
            </div>
          );
        })}
      </div>

      <div
        style={{
          position: "absolute",
          top: cardTop + HEADER_H + ROWS.length * ROW_H + 46,
          left: 0,
          right: 0,
          textAlign: "center",
          opacity: footer,
          fontFamily: SANS,
          fontSize: 25,
          color: COLOR.textDim,
        }}
      >
        Ships as a Claude Code plugin —{" "}
        <span style={{ color: COLOR.text, fontWeight: 600 }}>open source, no separate tier.</span>
      </div>
    </AbsoluteFill>
  );
};
