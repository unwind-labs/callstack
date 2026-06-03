import React from "react";
import { AbsoluteFill, useCurrentFrame, useVideoConfig, interpolate } from "remotion";
import { Background, TurnBlock, ramp } from "../ui";
import { COLOR, MONO, SANS } from "../theme";

const Card: React.FC<{
  mode: string;
  desc: string;
  accent: string;
  inherited: boolean;
  appear: number;
}> = ({ mode, desc, accent, inherited, appear }) => (
  <div
    style={{
      width: 460,
      opacity: appear,
      transform: `translateY(${interpolate(appear, [0, 1], [24, 0])}px)`,
    }}
  >
    <div
      style={{
        fontFamily: MONO,
        fontSize: 22,
        fontWeight: 700,
        color: accent,
        marginBottom: 14,
      }}
    >
      context = <span style={{ color: COLOR.text }}>&quot;{mode}&quot;</span>
    </div>
    <div
      style={{
        background: COLOR.panel,
        border: `1px solid ${accent}44`,
        borderRadius: 14,
        padding: 18,
        height: 320,
        display: "flex",
        flexDirection: "column",
        gap: 9,
        boxShadow: `0 24px 60px -30px ${accent}66`,
      }}
    >
      {inherited ? (
        <div
          style={{
            border: "1px dashed rgba(255,255,255,0.14)",
            borderRadius: 9,
            padding: "10px 12px",
            display: "flex",
            flexDirection: "column",
            gap: 5,
          }}
        >
          <div style={{ fontFamily: MONO, fontSize: 13, color: COLOR.textFaint }}>
            ⤴ inherited parent context
          </div>
          {[88, 64, 78].map((w, i) => (
            <div
              key={i}
              style={{
                height: 5,
                borderRadius: 5,
                width: `${w}%`,
                background: i % 2 ? "rgba(45,212,167,0.28)" : "rgba(77,141,246,0.28)",
              }}
            />
          ))}
        </div>
      ) : (
        <div
          style={{
            border: "1px dashed rgba(255,255,255,0.10)",
            borderRadius: 9,
            padding: "14px 12px",
            fontFamily: MONO,
            fontSize: 13,
            color: COLOR.textFaint,
            textAlign: "center",
          }}
        >
          ∅ empty — isolated session
        </div>
      )}
      <TurnBlock role="agent" height={86} width={424} bullets={4} seed={inherited ? 3 : 7} />
      <TurnBlock role="user" height={34} width={424} />
    </div>
    <div
      style={{
        marginTop: 16,
        fontFamily: SANS,
        fontSize: 21,
        lineHeight: 1.45,
        color: COLOR.textDim,
      }}
    >
      {desc}
    </div>
  </div>
);

export const S6_ForkFresh: React.FC = () => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const title = ramp(frame, 0.2 * fps, 0.6 * fps);
  const a = ramp(frame, 0.7 * fps, 0.5 * fps);
  const b = ramp(frame, 1.0 * fps, 0.5 * fps);

  return (
    <AbsoluteFill>
      <Background tint="rgba(185,139,251,0.10)" />
      <div
        style={{
          position: "absolute",
          top: 120,
          left: 0,
          right: 0,
          textAlign: "center",
          opacity: title,
        }}
      >
        <div style={{ fontFamily: SANS, fontSize: 58, fontWeight: 700, color: COLOR.text, letterSpacing: -1 }}>
          Fork or fresh — your choice.
        </div>
      </div>
      <AbsoluteFill style={{ justifyContent: "center", alignItems: "center" }}>
        <div style={{ display: "flex", gap: 90, marginTop: 60 }}>
          <Card
            mode="fork"
            accent={COLOR.call}
            inherited
            appear={a}
            desc="Inherits the parent's full transcript. One-line call, ~90% cheaper via prompt caching."
          />
          <Card
            mode="fresh"
            accent={COLOR.agent}
            inherited={false}
            appear={b}
            desc="Isolated like a subagent — but it can still nest calls and talk to the user."
          />
        </div>
      </AbsoluteFill>
    </AbsoluteFill>
  );
};
