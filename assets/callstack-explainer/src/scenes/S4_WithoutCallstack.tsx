import React from "react";
import { AbsoluteFill, useCurrentFrame } from "remotion";
import { Background, TurnBlock, ramp } from "../ui";
import { COLOR, MONO, SANS } from "../theme";
import { blockShape } from "../sim";

type Ev =
  | { t: number; kind: "task"; label: string }
  | { t: number; kind: "done"; label: string }
  | { t: number; kind: "turn"; role: "user" | "agent" };

// A single linear ReAct session that keeps nesting without ever popping.
const EVENTS: Ev[] = [
  { t: 24, kind: "task", label: "task 1" },
  { t: 40, kind: "turn", role: "agent" },
  { t: 62, kind: "turn", role: "user" },
  { t: 86, kind: "task", label: "task 1.1" },
  { t: 102, kind: "turn", role: "agent" },
  { t: 124, kind: "turn", role: "user" },
  { t: 150, kind: "task", label: "task 1.1.1" },
  { t: 166, kind: "turn", role: "agent" },
  { t: 188, kind: "turn", role: "user" },
  { t: 214, kind: "done", label: "task 1.1.1" },
  { t: 240, kind: "task", label: "task 1.1.2" },
  { t: 256, kind: "turn", role: "agent" },
  { t: 278, kind: "turn", role: "user" },
  { t: 304, kind: "task", label: "task 1.1.2.1" },
  { t: 320, kind: "turn", role: "agent" },
  { t: 342, kind: "turn", role: "user" },
  { t: 368, kind: "done", label: "task 1.1.2.1" },
];

const COL_W = 560;
const COL_H = 600;
const COL_TOP = 250;
const CONFUSE = 410;

export const S4_WithoutCallstack: React.FC = () => {
  const frame = useCurrentFrame();
  const head = ramp(frame, 6, 16);
  const confuse = ramp(frame, CONFUSE, 50);

  // Build animated items.
  type Item = { key: string; h: number; node: React.ReactNode };
  const items: Item[] = [];
  EVENTS.forEach((e, idx) => {
    const ap = ramp(frame, e.t, 11);
    if (ap <= 0) return;
    if (e.kind === "turn") {
      const shape = blockShape(e.role, idx);
      const h = shape.h * ap;
      items.push({
        key: `t${idx}`,
        h,
        node: (
          <div style={{ height: h, overflow: "hidden" }}>
            <TurnBlock
              role={e.role}
              height={shape.h}
              width={COL_W}
              appear={ap}
              bullets={shape.bullets}
              seed={idx}
            />
          </div>
        ),
      });
    } else {
      const isTask = e.kind === "task";
      const color = isTask ? COLOR.call : COLOR.agent;
      const h = 30 * ap;
      items.push({
        key: `m${idx}`,
        h,
        node: (
          <div
            style={{
              height: h,
              overflow: "hidden",
              display: "flex",
              alignItems: "center",
              gap: 12,
              opacity: ap,
            }}
          >
            <span style={{ fontFamily: MONO, fontSize: 17, fontWeight: 700, color }}>
              {isTask ? `▶ start ${e.label}` : `✓ ${e.label} complete`}
            </span>
            <div style={{ flex: 1, height: 1, background: `${color}44` }} />
          </div>
        ),
      });
    }
  });

  const content = items.reduce((s, it) => s + it.h, 0) + Math.max(0, items.length - 1) * 9;
  const scroll = Math.max(0, content - COL_H);

  // The agent's (in-context) guess at where it is.
  const liveTasks: string[] = [];
  EVENTS.forEach((e) => {
    if (e.t <= frame && e.kind === "task") liveTasks.push(e.label);
  });
  const tracked = liveTasks.length ? liveTasks[liveTasks.length - 1] : "—";
  const flicker = ["1.1?", "1.1.2?", "1.1.1?", "1.1.2.1?", "1 ??"];
  const guess =
    confuse > 0.5 ? flicker[Math.floor(frame / 5) % flicker.length] : tracked;

  return (
    <AbsoluteFill>
      <Background tint="rgba(77,141,246,0.08)" />

      {/* header */}
      <div
        style={{
          position: "absolute",
          top: 92,
          left: 0,
          right: 0,
          textAlign: "center",
          opacity: head,
        }}
      >
        <div style={{ fontFamily: SANS, fontSize: 40, fontWeight: 700, color: COLOR.text }}>
          Without callstack
        </div>
        <div style={{ fontFamily: MONO, fontSize: 19, color: COLOR.textDim, marginTop: 8 }}>
          one ReAct loop — every nested task piles into the same context
        </div>
      </div>

      {/* the growing column */}
      <div
        style={{
          position: "absolute",
          left: (1920 - COL_W) / 2,
          top: COL_TOP,
          width: COL_W,
          height: COL_H,
          overflow: "hidden",
        }}
      >
        <div
          style={{
            position: "absolute",
            left: 0,
            right: 0,
            top: 0,
            display: "flex",
            flexDirection: "column",
            gap: 9,
            transform: `translateY(${-scroll}px)`,
            filter: `blur(${confuse * 2}px)`,
          }}
        >
          {items.map((it) => (
            <React.Fragment key={it.key}>{it.node}</React.Fragment>
          ))}
        </div>
        {/* top scroll fade — early context slips out of reach */}
        <div
          style={{
            position: "absolute",
            top: 0,
            left: 0,
            right: 0,
            height: 90,
            background: `linear-gradient(${COLOR.bg0}, transparent)`,
            opacity: scroll > 4 ? 1 : 0,
          }}
        />
      </div>

      {/* tracked-location indicator */}
      <div
        style={{
          position: "absolute",
          top: COL_TOP,
          right: 230,
          width: 300,
          opacity: head,
        }}
      >
        <div style={{ fontFamily: MONO, fontSize: 14, color: COLOR.textFaint, marginBottom: 8 }}>
          agent&rsquo;s tracked position
        </div>
        <div
          style={{
            border: `1px solid ${confuse > 0.5 ? COLOR.danger : COLOR.panelEdge}55`,
            background: confuse > 0.5 ? COLOR.dangerSoft : "rgba(255,255,255,0.03)",
            borderRadius: 12,
            padding: "18px 20px",
            fontFamily: MONO,
            fontSize: 30,
            fontWeight: 700,
            color: confuse > 0.5 ? COLOR.danger : COLOR.text,
            textAlign: "center",
          }}
        >
          {guess}
        </div>
        <div
          style={{
            marginTop: 14,
            fontFamily: SANS,
            fontSize: 19,
            lineHeight: 1.4,
            color: COLOR.danger,
            opacity: confuse,
          }}
        >
          The session is too long. The agent loses the thread and can&rsquo;t reliably unwind.
        </div>
      </div>

      {/* red vignette on confusion */}
      <AbsoluteFill
        style={{
          pointerEvents: "none",
          opacity: confuse * (0.5 + 0.5 * Math.sin(frame / 6)),
          background:
            "radial-gradient(120% 90% at 50% 60%, transparent 50%, rgba(242,84,91,0.20) 100%)",
        }}
      />
    </AbsoluteFill>
  );
};
