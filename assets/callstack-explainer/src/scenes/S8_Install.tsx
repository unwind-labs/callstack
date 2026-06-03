import React from "react";
import { AbsoluteFill, useCurrentFrame, useVideoConfig, interpolate } from "remotion";
import { Background, ramp } from "../ui";
import { COLOR, MONO, SANS } from "../theme";

const CMDS = [
  "/plugin marketplace add unwind-labs/callstack",
  "/plugin install callstack@unwind-labs",
];

const tw = (text: string, start: number, frame: number, cps = 1.1): string => {
  const n = Math.floor((frame - start) * cps);
  if (n <= 0) return "";
  return text.slice(0, n);
};

export const S8_Install: React.FC = () => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const head = ramp(frame, 0.2 * fps, 0.6 * fps);
  const term = ramp(frame, 0.8 * fps, 0.5 * fps);
  const c0 = 1.3 * fps;
  const c1 = c0 + CMDS[0].length / 1.1 + 14;
  const tail = ramp(frame, 5.0 * fps, 0.6 * fps);

  return (
    <AbsoluteFill>
      <Background tint="rgba(45,212,167,0.10)" />
      <AbsoluteFill style={{ justifyContent: "center", alignItems: "center", textAlign: "center" }}>
        <div
          style={{
            fontFamily: MONO,
            fontSize: 20,
            letterSpacing: 4,
            textTransform: "uppercase",
            color: COLOR.agent,
            opacity: head,
            transform: `translateY(${interpolate(head, [0, 1], [10, 0])}px)`,
          }}
        >
          Available for Claude Code
        </div>
        <div
          style={{
            fontFamily: MONO,
            fontSize: 78,
            fontWeight: 700,
            letterSpacing: -2,
            color: COLOR.text,
            marginTop: 18,
            opacity: head,
          }}
        >
          <span style={{ color: COLOR.call }}>call</span>stack
        </div>

        {/* terminal with install commands */}
        <div
          style={{
            marginTop: 44,
            width: 880,
            opacity: term,
            transform: `translateY(${interpolate(term, [0, 1], [18, 0])}px)`,
          }}
        >
          <div
            style={{
              background: "#0a0d15",
              borderRadius: "12px 12px 0 0",
              border: `1px solid ${COLOR.panelEdge}`,
              padding: "10px 14px",
              display: "flex",
              alignItems: "center",
              gap: 8,
            }}
          >
            {[COLOR.danger, COLOR.call, COLOR.ok].map((c) => (
              <div key={c} style={{ width: 11, height: 11, borderRadius: 11, background: c, opacity: 0.7 }} />
            ))}
          </div>
          <div
            style={{
              background: "#0a0d15",
              border: `1px solid ${COLOR.panelEdge}`,
              borderTop: "none",
              borderRadius: "0 0 12px 12px",
              padding: "22px 26px",
              fontFamily: MONO,
              fontSize: 24,
              lineHeight: 1.9,
              textAlign: "left",
              color: COLOR.text,
            }}
          >
            {[c0, c1].map((start, i) => {
              if (frame < start) return null;
              const text = tw(CMDS[i], start, frame);
              const done = text.length >= CMDS[i].length;
              const cursor = !done && Math.floor(frame / 8) % 2 === 0 ? "▌" : "";
              return (
                <div key={i}>
                  <span style={{ color: COLOR.agent }}>❯ </span>
                  <span style={{ color: COLOR.call }}>{text.slice(0, text.indexOf(" ") > 0 ? text.indexOf(" ") : text.length)}</span>
                  <span>{text.slice(text.indexOf(" ") > 0 ? text.indexOf(" ") : text.length)}</span>
                  <span>{cursor}</span>
                </div>
              );
            })}
          </div>
        </div>

        <div
          style={{
            marginTop: 40,
            fontFamily: SANS,
            fontSize: 28,
            color: COLOR.textDim,
            opacity: tail,
          }}
        >
          Open source &amp; free to use{" "}
          <span style={{ color: COLOR.textFaint }}>·</span>{" "}
          <span style={{ color: COLOR.text, fontWeight: 600 }}>github.com/unwind-labs/callstack</span>
        </div>
      </AbsoluteFill>
    </AbsoluteFill>
  );
};
