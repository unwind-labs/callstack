import React from "react";
import {
  AbsoluteFill,
  interpolate,
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
