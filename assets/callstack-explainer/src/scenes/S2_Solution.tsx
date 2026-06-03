import React from "react";
import { AbsoluteFill, useCurrentFrame, useVideoConfig, interpolate } from "remotion";
import { Background, ramp } from "../ui";
import { COLOR, MONO, SANS } from "../theme";

export const S2_Solution: React.FC = () => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const mark = ramp(frame, 0.2 * fps, 0.7 * fps);
  const tag = ramp(frame, 0.7 * fps, 0.6 * fps);
  const sub = ramp(frame, 1.1 * fps, 0.6 * fps);
  const cmd = ramp(frame, 1.6 * fps, 0.6 * fps);

  return (
    <AbsoluteFill>
      <Background tint="rgba(242,169,59,0.12)" />
      <AbsoluteFill style={{ justifyContent: "center", alignItems: "center", textAlign: "center" }}>
        {/* stacked-frames glyph */}
        <div style={{ opacity: mark, marginBottom: 40, display: "flex", gap: 0 }}>
          {[0, 1, 2].map((i) => (
            <div
              key={i}
              style={{
                width: 64,
                height: 64,
                marginLeft: i === 0 ? 0 : -10,
                marginTop: i * 14,
                borderRadius: 12,
                background: `${COLOR.call}${["1f", "2c", "3a"][i]}`,
                border: `1.5px solid ${COLOR.call}${["55", "77", "99"][i]}`,
                transform: `translateY(${interpolate(mark, [0, 1], [20 + i * 8, 0])}px)`,
              }}
            />
          ))}
        </div>
        <div
          style={{
            fontFamily: MONO,
            fontSize: 96,
            fontWeight: 700,
            letterSpacing: -2,
            color: COLOR.text,
            opacity: mark,
            transform: `translateY(${interpolate(mark, [0, 1], [18, 0])}px)`,
          }}
        >
          <span style={{ color: COLOR.call }}>call</span>stack
        </div>
        <div
          style={{
            fontFamily: SANS,
            fontSize: 40,
            fontWeight: 600,
            color: COLOR.text,
            marginTop: 22,
            opacity: tag,
            transform: `translateY(${interpolate(tag, [0, 1], [14, 0])}px)`,
          }}
        >
          A deterministic call stack for agents.
        </div>
        <div
          style={{
            fontFamily: SANS,
            fontSize: 28,
            color: COLOR.textDim,
            marginTop: 16,
            maxWidth: 1000,
            opacity: sub,
          }}
        >
          It tracks deeply nested workflows reliably — so the LLM doesn&rsquo;t have to.
        </div>
        <div
          style={{
            marginTop: 30,
            opacity: cmd,
            transform: `translateY(${interpolate(cmd, [0, 1], [10, 0])}px)`,
            display: "flex",
            alignItems: "center",
            gap: 14,
            fontFamily: MONO,
            fontSize: 20,
            color: COLOR.textDim,
          }}
        >
          <span
            style={{
              color: COLOR.call,
              background: COLOR.callSoft,
              border: `1px solid ${COLOR.call}55`,
              borderRadius: 8,
              padding: "6px 14px",
              fontWeight: 700,
            }}
          >
            /call
          </span>
          <span>
            fork the session <span style={{ color: COLOR.textFaint }}>·</span> run the task{" "}
            <span style={{ color: COLOR.textFaint }}>·</span> return a compact result
          </span>
        </div>
      </AbsoluteFill>
    </AbsoluteFill>
  );
};
