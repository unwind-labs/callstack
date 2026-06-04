import React from "react";
import {
  AbsoluteFill,
  interpolate,
  interpolateColors,
  useCurrentFrame,
  useVideoConfig,
  Easing,
} from "remotion";
import { COLOR, MONO, SANS, BLOCK_W } from "./theme";
import { hash01 } from "./sim";

const EASE_OUT = Easing.bezier(0.16, 1, 0.3, 1);
const EASE_IN = Easing.in(Easing.cubic);

// 0 -> 1 -> 0 envelope for a clip of length `total`, given local frame.
export const fadeInOut = (
  frame: number,
  total: number,
  inDur = 14,
  outDur = 14,
): number => {
  const fin = interpolate(frame, [0, inDur], [0, 1], {
    easing: EASE_OUT,
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });
  const fout = interpolate(frame, [total - outDur, total], [1, 0], {
    easing: EASE_IN,
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });
  return Math.min(fin, fout);
};

// Simple eased 0->1 ramp.
export const ramp = (frame: number, start: number, dur: number): number =>
  interpolate(frame, [start, start + dur], [0, 1], {
    easing: EASE_OUT,
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });

export const Background: React.FC<{ tint?: string }> = ({ tint }) => {
  const frame = useCurrentFrame();
  const drift = Math.sin(frame / 90) * 40;
  const drift2 = Math.cos(frame / 70) * 30;
  return (
    <AbsoluteFill style={{ backgroundColor: COLOR.bg0 }}>
      <AbsoluteFill
        style={{
          background: `radial-gradient(120% 90% at ${50 + drift / 20}% -10%, ${COLOR.bg1} 0%, ${COLOR.bg0} 60%)`,
        }}
      />
      {/* faint grid */}
      <AbsoluteFill
        style={{
          backgroundImage: `linear-gradient(${COLOR.grid} 1px, transparent 1px), linear-gradient(90deg, ${COLOR.grid} 1px, transparent 1px)`,
          backgroundSize: "64px 64px",
          maskImage:
            "radial-gradient(120% 100% at 50% 40%, black 30%, transparent 80%)",
          WebkitMaskImage:
            "radial-gradient(120% 100% at 50% 40%, black 30%, transparent 80%)",
        }}
      />
      {/* drifting accent glow */}
      <AbsoluteFill
        style={{
          background: `radial-gradient(40% 40% at ${30 + drift}px ${20 + drift2}%, ${tint ?? "rgba(77,141,246,0.10)"} 0%, transparent 70%)`,
        }}
      />
    </AbsoluteFill>
  );
};

export const Caption: React.FC<{
  children: React.ReactNode;
  total: number;
  accent?: string;
}> = ({ children, total, accent }) => {
  const frame = useCurrentFrame();
  const op = fadeInOut(frame, total, 16, 16);
  const y = interpolate(op, [0, 1], [16, 0]);
  return (
    <div
      style={{
        position: "absolute",
        left: 0,
        right: 0,
        bottom: 78,
        display: "flex",
        justifyContent: "center",
        opacity: op,
        transform: `translateY(${y}px)`,
      }}
    >
      <div
        style={{
          maxWidth: 1180,
          textAlign: "center",
          fontFamily: SANS,
          fontSize: 33,
          lineHeight: 1.4,
          fontWeight: 500,
          color: COLOR.text,
          letterSpacing: -0.2,
          padding: "0 40px",
          borderLeft: accent ? `3px solid ${accent}` : undefined,
        }}
      >
        {children}
      </div>
    </div>
  );
};

// A small monospace pill/label.
export const Pill: React.FC<{
  children: React.ReactNode;
  color: string;
  soft: string;
  style?: React.CSSProperties;
}> = ({ children, color, soft, style }) => (
  <span
    style={{
      fontFamily: MONO,
      fontSize: 17,
      fontWeight: 600,
      color,
      background: soft,
      border: `1px solid ${color}44`,
      borderRadius: 7,
      padding: "4px 10px",
      whiteSpace: "nowrap",
      ...style,
    }}
  >
    {children}
  </span>
);

// A single conversation turn block. Agent turns render a multi-point response
// (1–6 bullets — more tokens, taller); user turns are a short message.
export const TurnBlock: React.FC<{
  role: "user" | "agent";
  height: number;
  width?: number;
  appear?: number; // 0..1 entrance progress
  dim?: boolean;
  bullets?: number;
  seed?: number;
}> = ({ role, height, width = BLOCK_W, appear = 1, dim = false, bullets = 0, seed = 0 }) => {
  const color = role === "user" ? COLOR.user : COLOR.agent;
  const soft = role === "user" ? COLOR.userSoft : COLOR.agentSoft;
  const op = (dim ? 0.34 : 1) * appear;
  const x = interpolate(appear, [0, 1], [-22, 0]);
  const showBullets = role === "agent" && bullets > 0 && !dim;
  return (
    <div
      style={{
        width,
        height,
        borderRadius: 9,
        background: dim ? "rgba(255,255,255,0.025)" : soft,
        border: `1px solid ${dim ? "rgba(255,255,255,0.06)" : color + "55"}`,
        borderLeft: `3px solid ${color}${dim ? "66" : ""}`,
        display: "flex",
        alignItems: showBullets ? "flex-start" : "center",
        gap: 9,
        padding: showBullets ? "9px 12px" : "0 12px",
        opacity: op,
        transform: `translateX(${x}px)`,
        boxSizing: "border-box",
        overflow: "hidden",
      }}
    >
      <div
        style={{
          width: 9,
          height: 9,
          borderRadius: 9,
          background: color,
          flexShrink: 0,
          marginTop: showBullets ? 2 : 0,
          boxShadow: dim ? "none" : `0 0 8px ${color}88`,
        }}
      />
      {dim ? null : showBullets ? (
        <div style={{ display: "flex", flexDirection: "column", gap: 6, flex: 1 }}>
          {Array.from({ length: bullets }).map((_, i) => (
            <div key={i} style={{ display: "flex", alignItems: "center", gap: 7 }}>
              <div
                style={{
                  width: 4,
                  height: 4,
                  background: color,
                  transform: "rotate(45deg)",
                  flexShrink: 0,
                }}
              />
              <div
                style={{
                  height: 4,
                  borderRadius: 4,
                  background: `${color}55`,
                  width: `${44 + Math.floor(hash01(seed * 13 + i * 7) * 48)}%`,
                }}
              />
            </div>
          ))}
        </div>
      ) : (
        <div style={{ display: "flex", flexDirection: "column", gap: 4, flex: 1 }}>
          {[78, 52].map((w, i) => (
            <div
              key={i}
              style={{ height: 4, borderRadius: 4, background: `${color}55`, width: `${w}%` }}
            />
          ))}
        </div>
      )}
    </div>
  );
};

// Big centered scene title with optional kicker line above.
export const TitleBlock: React.FC<{
  kicker?: string;
  kickerColor?: string;
  title: React.ReactNode;
  sub?: React.ReactNode;
}> = ({ kicker, kickerColor, title, sub }) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const k = ramp(frame, 0, 0.4 * fps);
  const t = ramp(frame, 0.18 * fps, 0.6 * fps);
  const s = ramp(frame, 0.5 * fps, 0.6 * fps);
  return (
    <AbsoluteFill
      style={{
        justifyContent: "center",
        alignItems: "center",
        textAlign: "center",
        padding: "0 140px",
      }}
    >
      {kicker ? (
        <div
          style={{
            fontFamily: MONO,
            fontSize: 22,
            letterSpacing: 4,
            textTransform: "uppercase",
            color: kickerColor ?? COLOR.call,
            opacity: k,
            transform: `translateY(${interpolate(k, [0, 1], [10, 0])}px)`,
            marginBottom: 26,
          }}
        >
          {kicker}
        </div>
      ) : null}
      <div
        style={{
          fontFamily: SANS,
          fontSize: 70,
          fontWeight: 700,
          lineHeight: 1.08,
          letterSpacing: -1.5,
          color: COLOR.text,
          opacity: t,
          transform: `translateY(${interpolate(t, [0, 1], [18, 0])}px)`,
          maxWidth: 1500,
        }}
      >
        {title}
      </div>
      {sub ? (
        <div
          style={{
            fontFamily: SANS,
            fontSize: 30,
            fontWeight: 400,
            lineHeight: 1.4,
            color: COLOR.textDim,
            opacity: s,
            marginTop: 28,
            maxWidth: 1180,
          }}
        >
          {sub}
        </div>
      ) : null}
    </AbsoluteFill>
  );
};

// ── Cross-cutting motif ──────────────────────────────────────────────────────
// "1.2.2 is done — what do I resume?" — the recurring control-flow indicator,
// built around the *return* problem (where LLMs actually fail).
//   flip = 0 → MODEL / flat context: every task sits at the same indent with no
//             parent links. The model holds a probability over which caller to
//             resume (1.2 highest, but < 100%), the %s shimmer, and the highlight
//             is a SAMPLE from that distribution — resting on 1.2 most often and
//             landing on a wrong task (red) proportionally to its probability.
//   flip = 1 → HARNESS / call stack: completed siblings have popped; only the
//             live chain (1 → 1.2 → 1.2.2) remains, nested. The parent is simply
//             the frame above — resume 1.2. One answer, steady. Ok green.
// Sampling/shimmer use hash01 (deterministic, render-stable).
export type StackTask = { id: string; depth: number; status: "done" | "active" | "current" };

const RESUME_TASKS: StackTask[] = [
  { id: "1", depth: 0, status: "active" },
  { id: "1.1", depth: 1, status: "done" },
  { id: "1.1.1", depth: 2, status: "done" },
  { id: "1.2", depth: 1, status: "active" },
  { id: "1.2.1", depth: 2, status: "done" },
  { id: "1.2.2", depth: 2, status: "current" },
];
// The model's resume-target distribution: every task except the just-finished
// 1.2.2 is a candidate caller. 1.2 (the true parent) is likeliest — but not 1,
// so sampling from it lands elsewhere periodically.
const RESUME_CANDS = [
  { id: "1", p: 0.11 },
  { id: "1.1", p: 0.03 },
  { id: "1.1.1", p: 0.04 },
  { id: "1.2", p: 0.67 },
  { id: "1.2.1", p: 0.15 },
];

const RED_BORDER = "rgba(242,84,91,0.34)";
const GREEN_BORDER = "rgba(45,212,167,0.34)";
const RED_BG = "rgba(242,84,91,0.06)";
const GREEN_BG = "rgba(45,212,167,0.06)";

export const StackPointer: React.FC<{ frame: number; flip: number; width?: number }> = ({
  frame,
  flip,
  width = 580,
}) => {
  const ROW_H = 30;
  // Per-frame display probabilities — they shimmer to show the model's shifting
  // uncertainty — normalized to sum ~100%.
  const rawP = RESUME_CANDS.map((c, i) =>
    Math.max(0.02, c.p + (hash01(frame * 0.4 + i * 13) - 0.5) * 0.05),
  );
  const sumP = rawP.reduce((s, v) => s + v, 0);
  const pctById: Record<string, number> = {};
  RESUME_CANDS.forEach((c, i) => {
    pctById[c.id] = (rawP[i] / sumP) * 100;
  });
  // The highlight is a SAMPLE from the (base) distribution, redrawn every ~10
  // frames — proportional to probability, so it rests on the likeliest caller
  // (1.2) most often but lands elsewhere periodically. The harness locks on 1.2.
  const bucket = Math.floor(frame / 10);
  let guessId = RESUME_CANDS[0].id;
  if (flip > 0.5) {
    guessId = "1.2";
  } else {
    const r = hash01(bucket * 1.37);
    let acc = 0;
    for (const c of RESUME_CANDS) {
      acc += c.p;
      if (r <= acc) {
        guessId = c.id;
        break;
      }
    }
  }
  const correct = guessId === "1.2";
  const hlColor = correct ? COLOR.ok : COLOR.danger;
  const jx = (1 - flip) * (hash01(frame * 1.7) - 0.5) * 5;
  const curColor = interpolateColors(flip, [0, 1], [COLOR.danger, COLOR.ok]);

  return (
    <div style={{ width, fontFamily: MONO, textAlign: "left" }}>
      {/* header crossfade */}
      <div style={{ position: "relative", height: 30, marginBottom: 16 }}>
        <span
          style={{
            position: "absolute",
            left: 0,
            whiteSpace: "nowrap",
            fontFamily: SANS,
            fontSize: 23,
            fontWeight: 700,
            color: COLOR.danger,
            opacity: 1 - flip,
          }}
        >
          ◆ Without callstack — unreliable control flow
        </span>
        <span
          style={{
            position: "absolute",
            left: 0,
            whiteSpace: "nowrap",
            fontFamily: SANS,
            fontSize: 23,
            fontWeight: 700,
            color: COLOR.ok,
            opacity: flip,
          }}
        >
          ■ With callstack — reliable control flow
        </span>
      </div>

      {/* task list (flat) → live stack (nested) */}
      <div
        style={{
          border: `1px solid ${interpolateColors(flip, [0, 1], [RED_BORDER, GREEN_BORDER])}`,
          background: interpolateColors(flip, [0, 1], [RED_BG, GREEN_BG]),
          borderRadius: 12,
          padding: "12px 16px",
          display: "flex",
          flexDirection: "column",
          gap: 5,
        }}
      >
        {RESUME_TASKS.map((t) => {
          const live = t.status !== "done"; // ancestors + current stay on the stack
          const collapse = live ? 0 : flip; // completed siblings pop away
          const h = ROW_H * (1 - collapse);
          if (1 - collapse <= 0.01) return null;
          const indent = t.depth * 30 * flip; // flat at 0, nested at 1
          const rowColor = live
            ? interpolateColors(flip, [0, 1], [COLOR.textDim, COLOR.ok])
            : COLOR.textFaint;
          const hot = t.id === guessId; // the (wandering) resume target
          return (
            <div
              key={t.id}
              style={{
                height: h,
                overflow: "hidden",
                opacity: 1 - collapse,
                display: "flex",
                alignItems: "center",
                gap: 10,
                marginLeft: indent,
                paddingLeft: 8,
                borderLeft: `3px solid ${hot ? hlColor : "transparent"}`,
                borderRadius: 6,
                background: hot
                  ? correct
                    ? "rgba(45,212,167,0.14)"
                    : COLOR.dangerSoft
                  : "transparent",
                transform: hot && flip < 0.5 ? `translateX(${jx}px)` : "none",
              }}
            >
              <span style={{ width: 14, flexShrink: 0, color: COLOR.ok, opacity: flip }}>
                {t.depth > 0 ? "└" : ""}
              </span>
              <span
                style={{
                  fontSize: 16,
                  whiteSpace: "nowrap",
                  flexShrink: 0,
                  color: rowColor,
                  fontWeight: t.status === "current" ? 700 : 400,
                }}
              >
                task {t.id}
              </span>
              <span
                style={{
                  fontSize: 13,
                  whiteSpace: "nowrap",
                  flexShrink: 0,
                  color:
                    t.status === "done"
                      ? COLOR.textFaint
                      : t.status === "current"
                        ? curColor
                        : COLOR.textDim,
                }}
              >
                {t.status === "done"
                  ? "✓ done"
                  : t.status === "current"
                    ? "← just finished"
                    : "in progress"}
              </span>
              <span
                style={{
                  marginLeft: "auto",
                  flexShrink: 0,
                  display: "flex",
                  alignItems: "center",
                  gap: 12,
                }}
              >
                {t.id !== "1.2.2" ? (
                  <span
                    style={{ display: "flex", alignItems: "center", gap: 8, opacity: 1 - flip }}
                  >
                    <span
                      style={{
                        width: 58,
                        height: 5,
                        borderRadius: 5,
                        background: "rgba(255,255,255,0.08)",
                        overflow: "hidden",
                      }}
                    >
                      <span
                        style={{
                          display: "block",
                          height: "100%",
                          width: `${pctById[t.id]}%`,
                          background: t.id === "1.2" ? COLOR.ok : COLOR.textDim,
                          borderRadius: 5,
                        }}
                      />
                    </span>
                    <span
                      style={{
                        width: 32,
                        textAlign: "right",
                        fontSize: 12,
                        color: t.id === "1.2" ? COLOR.ok : COLOR.textFaint,
                      }}
                    >
                      {Math.round(pctById[t.id])}%
                    </span>
                  </span>
                ) : null}
                <span
                  style={{
                    width: 78,
                    textAlign: "right",
                    whiteSpace: "nowrap",
                    fontSize: 13,
                    fontWeight: 700,
                    color: hlColor,
                    opacity: hot ? 1 : 0,
                  }}
                >
                  {flip > 0.5 ? "↩ resume ✓" : "↩ resume?"}
                </span>
              </span>
            </div>
          );
        })}
      </div>

      {/* the question the highlight answers */}
      <div style={{ marginTop: 12, fontSize: 14, color: COLOR.textDim }}>
        task 1.2.2 just finished — where does control return?
      </div>
    </div>
  );
};

// A compact echo of the motif's two states, for scenes that already render a
// full stack (S5/S7) and just need the one-line callback in the shared language.
export const MotifBadge: React.FC<{ state: "model" | "harness" }> = ({ state }) => {
  const harness = state === "harness";
  const color = harness ? COLOR.ok : COLOR.danger;
  return (
    <span
      style={{
        fontFamily: MONO,
        fontSize: 15,
        fontWeight: 700,
        color,
        background: harness ? "rgba(45,212,167,0.12)" : COLOR.dangerSoft,
        border: `1px solid ${color}66`,
        borderRadius: 8,
        padding: "6px 13px",
        whiteSpace: "nowrap",
      }}
    >
      {harness ? "■ harness tracks the stack" : "◆ model is guessing"}
    </span>
  );
};
