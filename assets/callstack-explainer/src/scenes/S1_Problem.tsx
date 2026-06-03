import React from "react";
import { AbsoluteFill, useCurrentFrame, useVideoConfig, interpolate } from "remotion";
import { Background, TitleBlock, ramp } from "../ui";
import { COLOR, MONO } from "../theme";

export const S1_Problem: React.FC = () => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();

  // A context bar that fills up and overflows into red.
  const fill = interpolate(frame, [1.4 * fps, 5.2 * fps], [0, 1], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });
  const over = Math.max(0, fill - 0.74) / 0.26;
  const motif = ramp(frame, 1.2 * fps, 0.6 * fps);

  return (
    <AbsoluteFill>
      <Background tint="rgba(242,84,91,0.10)" />
      <TitleBlock
        kicker="The problem"
        kickerColor={COLOR.danger}
        title="Complex workflows break agents."
        sub="One linear context can't hold a deeply nested workflow — it rots, compaction drops detail, and the agent loses the thread."
      />
      <div
        style={{
          position: "absolute",
          left: 0,
          right: 0,
          bottom: 132,
          display: "flex",
          justifyContent: "center",
          opacity: motif,
        }}
      >
        <div style={{ width: 760 }}>
          <div
            style={{
              display: "flex",
              justifyContent: "space-between",
              fontFamily: MONO,
              fontSize: 15,
              color: COLOR.textDim,
              marginBottom: 8,
            }}
          >
            <span>context window</span>
            <span style={{ color: over > 0.1 ? COLOR.danger : COLOR.textDim }}>
              {over > 0.1 ? "losing track…" : "filling up"}
            </span>
          </div>
          <div
            style={{
              height: 16,
              borderRadius: 8,
              background: "rgba(255,255,255,0.06)",
              border: `1px solid ${COLOR.panelEdge}`,
              overflow: "hidden",
            }}
          >
            <div
              style={{
                height: "100%",
                width: `${Math.min(100, fill * 100)}%`,
                background: `linear-gradient(90deg, ${COLOR.user}, ${COLOR.agent} 55%, ${COLOR.danger} 100%)`,
                opacity: 0.55 + over * 0.45,
              }}
            />
          </div>
        </div>
      </div>
    </AbsoluteFill>
  );
};
