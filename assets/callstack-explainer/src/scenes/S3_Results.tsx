import React from "react";
import { AbsoluteFill, useCurrentFrame, useVideoConfig, interpolate, Easing } from "remotion";
import { Background, ramp, Pill } from "../ui";
import { COLOR, MONO, SANS } from "../theme";

const WITHOUT = [100, 100, 100, 100, 66, 33, 0];
const WITH = [100, 100, 100, 100, 100, 100, 100];

const CHART_W = 1180;
const CHART_H = 430;
const EASE = Easing.bezier(0.16, 1, 0.3, 1);

export const S3_Results: React.FC = () => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const head = ramp(frame, 0.2 * fps, 0.6 * fps);
  const chart = ramp(frame, 0.9 * fps, 0.5 * fps);

  const groupW = CHART_W / 7;
  const barW = 52;

  const bar = (depthIdx: number, val: number, series: 0 | 1) => {
    const start = 1.2 * fps + depthIdx * 4 + series * 2;
    const grow = interpolate(frame, [start, start + 14], [0, 1], {
      easing: EASE,
      extrapolateLeft: "clamp",
      extrapolateRight: "clamp",
    });
    const h = (val / 100) * CHART_H * grow;
    const color = series === 1 ? COLOR.agent : val === 0 ? COLOR.danger : COLOR.user;
    const cx = depthIdx * groupW + groupW / 2 + (series === 0 ? -barW / 2 - 4 : barW / 2 + 4);
    return (
      <div
        key={`${depthIdx}-${series}`}
        style={{ position: "absolute", left: cx - barW / 2, bottom: 0, width: barW }}
      >
        {val === 0 && grow > 0.6 ? (
          <div
            style={{
              position: "absolute",
              bottom: 6,
              width: barW,
              textAlign: "center",
              fontFamily: MONO,
              fontSize: 18,
              fontWeight: 700,
              color: COLOR.danger,
            }}
          >
            ✕
          </div>
        ) : (
          <div
            style={{
              position: "absolute",
              bottom: h + 6,
              width: barW,
              textAlign: "center",
              fontFamily: MONO,
              fontSize: 15,
              color,
              opacity: grow,
            }}
          >
            {val}
          </div>
        )}
        <div
          style={{
            width: barW,
            height: h,
            borderRadius: "6px 6px 0 0",
            background: `linear-gradient(${color}, ${color}77)`,
            boxShadow: series === 1 ? `0 0 22px ${color}44` : "none",
          }}
        />
      </div>
    );
  };

  return (
    <AbsoluteFill>
      <Background tint="rgba(45,212,167,0.10)" />
      <div
        style={{
          position: "absolute",
          top: 96,
          left: 0,
          right: 0,
          textAlign: "center",
          opacity: head,
          padding: "0 120px",
        }}
      >
        <div
          style={{
            fontFamily: MONO,
            fontSize: 20,
            letterSpacing: 3,
            textTransform: "uppercase",
            color: COLOR.agent,
            marginBottom: 14,
          }}
        >
          The result
        </div>
        <div style={{ fontFamily: SANS, fontSize: 50, fontWeight: 700, color: COLOR.text, letterSpacing: -1 }}>
          Tested on workflows with 500+ nodes, 7 levels deep.
        </div>
      </div>

      {/* chart */}
      <div
        style={{
          position: "absolute",
          left: (1920 - CHART_W) / 2,
          top: 300,
          width: CHART_W,
          opacity: chart,
        }}
      >
        {/* gridlines */}
        {[0, 25, 50, 75, 100].map((g) => (
          <div
            key={g}
            style={{
              position: "absolute",
              left: -46,
              right: 0,
              bottom: (g / 100) * CHART_H,
              height: 1,
              background: "rgba(255,255,255,0.06)",
            }}
          >
            <span
              style={{
                position: "absolute",
                left: -48,
                top: -10,
                fontFamily: MONO,
                fontSize: 13,
                color: COLOR.textFaint,
                width: 40,
                textAlign: "right",
              }}
            >
              {g}%
            </span>
          </div>
        ))}
        <div style={{ position: "relative", height: CHART_H }}>
          {WITHOUT.map((v, i) => bar(i, v, 0))}
          {WITH.map((v, i) => bar(i, v, 1))}
        </div>
        {/* x labels */}
        <div style={{ position: "relative", height: 34, marginTop: 6 }}>
          {WITHOUT.map((_, i) => (
            <div
              key={i}
              style={{
                position: "absolute",
                left: i * groupW,
                width: groupW,
                textAlign: "center",
                fontFamily: MONO,
                fontSize: 16,
                color: COLOR.textDim,
              }}
            >
              depth {i + 1}
            </div>
          ))}
        </div>
      </div>

      {/* legend + footnote */}
      <div
        style={{
          position: "absolute",
          bottom: 70,
          left: 0,
          right: 0,
          display: "flex",
          justifyContent: "center",
          alignItems: "center",
          gap: 22,
          opacity: chart,
        }}
      >
        <Pill color={COLOR.agent} soft={COLOR.agentSoft}>● with callstack — 100% success</Pill>
        <Pill color={COLOR.user} soft={COLOR.userSoft}>● without — fails by depth 5, zero at 7</Pill>
        <Pill color={COLOR.textDim} soft="rgba(255,255,255,0.05)">paper coming soon</Pill>
      </div>
    </AbsoluteFill>
  );
};
