import React from "react";
import { AbsoluteFill, interpolate, Easing } from "remotion";
import {
  COLOR,
  MONO,
  SANS,
  BLOCK_W,
  BLOCK_GAP,
  FRAME_W,
  FRAME_GAP,
  STAIR_Y,
  WIDTH,
} from "./theme";
import { cameraDepth, Sim, SimFrame } from "./sim";
import { TurnBlock, ramp } from "./ui";

const EASE_IN = Easing.in(Easing.cubic);

const BODY_MAX_H = 452;
const HEADER_H = 46;
const FRAME_PAD = 14;
const CALL_H = 32;

type Item = {
  key: string;
  h: number;
  kind: "inherited" | "block" | "call" | "result";
  childId?: string;
  node: React.ReactNode;
};

// Build a frame's body items (inherited band, then blocks / /call messages /
// return pills in chronological order) with animated heights, plus the scroll
// offset. Shared by the panel renderer and the connector anchoring.
function frameItems(f: SimFrame, frame: number): { items: Item[]; scroll: number } {
  const items: Item[] = [];

  if (f.inherited > 0) {
    const shown = Math.min(f.inherited, 5);
    items.push({
      key: "inh",
      h: 78,
      kind: "inherited",
      node: (
        <div
          style={{
            border: `1px dashed rgba(255,255,255,0.12)`,
            borderRadius: 9,
            padding: "8px 10px",
            display: "flex",
            flexDirection: "column",
            gap: 5,
            background: "rgba(255,255,255,0.015)",
          }}
        >
          <div style={{ fontFamily: MONO, fontSize: 13, color: COLOR.textFaint }}>
            ⤴ copied context · {f.inherited} turns
          </div>
          <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
            {Array.from({ length: shown }).map((_, i) => (
              <div
                key={i}
                style={{
                  height: 5,
                  borderRadius: 5,
                  background: i % 2 === 0 ? "rgba(77,141,246,0.28)" : "rgba(45,212,167,0.28)",
                  width: `${[88, 64, 80, 56, 72][i]}%`,
                }}
              />
            ))}
          </div>
        </div>
      ),
    });
  }

  type Evt =
    | { sort: number; kind: "block"; b: SimFrame["blocks"][number] }
    | { sort: number; kind: "call"; c: SimFrame["calls"][number] }
    | { sort: number; kind: "result"; r: SimFrame["results"][number] };
  const evts: Evt[] = [];
  for (const b of f.blocks) evts.push({ sort: b.bornAt, kind: "block", b });
  for (const c of f.calls) evts.push({ sort: c.bornAt, kind: "call", c });
  for (const r of f.results) evts.push({ sort: r.bornAt, kind: "result", r });
  evts.sort((a, b) => a.sort - b.sort);

  for (const e of evts) {
    if (e.kind === "block") {
      const ap = ramp(frame, e.b.bornAt, 11);
      if (ap <= 0) continue;
      items.push({
        key: e.b.key,
        h: e.b.h * ap,
        kind: "block",
        node: (
          <div style={{ height: e.b.h * ap, overflow: "hidden" }}>
            <TurnBlock role={e.b.role} height={e.b.h} appear={ap} bullets={e.b.bullets} seed={e.b.seed} />
          </div>
        ),
      });
    } else if (e.kind === "call") {
      const ap = ramp(frame, e.c.bornAt, 11);
      if (ap <= 0) continue;
      items.push({
        key: e.c.key,
        h: CALL_H * ap,
        kind: "call",
        childId: e.c.childId,
        node: (
          <div
            style={{
              height: CALL_H * ap,
              overflow: "hidden",
              opacity: ap,
              transform: `translateX(${interpolate(ap, [0, 1], [14, 0])}px)`,
            }}
          >
            <div
              style={{
                height: CALL_H,
                width: BLOCK_W,
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
              <span
                style={{
                  color: COLOR.text,
                  opacity: 0.85,
                  whiteSpace: "nowrap",
                  overflow: "hidden",
                  textOverflow: "ellipsis",
                }}
              >
                {e.c.label}
              </span>
            </div>
          </div>
        ),
      });
    } else {
      const ap = ramp(frame, e.r.bornAt, 13);
      if (ap <= 0) continue;
      items.push({
        key: e.r.key,
        h: 34 * ap,
        kind: "result",
        node: (
          <div
            style={{
              height: 34 * ap,
              overflow: "hidden",
              opacity: ap,
              transform: `translateX(${interpolate(ap, [0, 1], [14, 0])}px)`,
            }}
          >
            <div
              style={{
                height: 34,
                width: BLOCK_W,
                borderRadius: 9,
                background: COLOR.resultSoft,
                border: `1px solid ${COLOR.result}66`,
                display: "flex",
                alignItems: "center",
                gap: 8,
                padding: "0 12px",
                boxSizing: "border-box",
                fontFamily: MONO,
                fontSize: 14,
                color: COLOR.result,
              }}
            >
              <span style={{ fontWeight: 700 }}>↳ return</span>
              <span style={{ color: COLOR.text, opacity: 0.85 }}>{e.r.label}</span>
            </div>
          </div>
        ),
      });
    }
  }

  const content = items.reduce((s, it) => s + it.h, 0) + Math.max(0, items.length - 1) * BLOCK_GAP;
  const scroll = Math.max(0, content - BODY_MAX_H);
  return { items, scroll };
}

const FramePanel: React.FC<{ f: SimFrame; frame: number; active: boolean; dist: number }> = ({
  f,
  frame,
  active,
  dist,
}) => {
  const appear = ramp(frame, f.bornAt, 16);
  const exit =
    f.returnedAt === null
      ? 0
      : interpolate(frame, [f.returnedAt, f.returnedAt + 16], [0, 1], {
          easing: EASE_IN,
          extrapolateLeft: "clamp",
          extrapolateRight: "clamp",
        });

  const isRoot = f.depth === 0;
  const accent = isRoot ? COLOR.user : COLOR.call;
  const { items, scroll } = frameItems(f, frame);

  const baseOpacity = active ? 1 : Math.max(0.32, 1 - dist * 0.26);
  const opacity = appear * (1 - exit) * baseOpacity;
  const ty = interpolate(appear, [0, 1], [26, 0]) - exit * 60;
  const scale = interpolate(appear, [0, 1], [0.95, 1]);

  return (
    <div
      style={{
        position: "absolute",
        width: FRAME_W,
        opacity,
        transform: `translateY(${ty}px) scale(${scale})`,
        transformOrigin: "top center",
      }}
    >
      {/* header */}
      <div
        style={{
          height: HEADER_H,
          borderRadius: "12px 12px 0 0",
          background: active ? `${accent}1f` : "rgba(255,255,255,0.03)",
          border: `1px solid ${active ? accent + "55" : COLOR.panelEdge}`,
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
          {isRoot ? "ROOT" : `/call`}
        </span>
        <span style={{ fontFamily: SANS, fontSize: 18, fontWeight: 600, color: COLOR.text }}>
          {f.label}
        </span>
        <span style={{ fontFamily: MONO, fontSize: 13, color: COLOR.textFaint, marginLeft: "auto" }}>
          depth {f.depth}
        </span>
      </div>
      {/* body */}
      <div
        style={{
          height: BODY_MAX_H,
          background: COLOR.panel,
          border: `1px solid ${active ? accent + "44" : COLOR.panelEdge}`,
          borderTop: "none",
          borderRadius: "0 0 12px 12px",
          padding: FRAME_PAD,
          boxSizing: "border-box",
          overflow: "hidden",
          position: "relative",
          boxShadow: active ? `0 24px 60px -24px ${accent}55` : "none",
        }}
      >
        <div
          style={{
            position: "absolute",
            left: FRAME_PAD,
            right: FRAME_PAD,
            top: FRAME_PAD,
            display: "flex",
            flexDirection: "column",
            gap: BLOCK_GAP,
            transform: `translateY(${-scroll}px)`,
          }}
        >
          {items.map((it) => (
            <React.Fragment key={it.key}>{it.node}</React.Fragment>
          ))}
        </div>
        {/* top fade when scrolled */}
        <div
          style={{
            position: "absolute",
            top: 0,
            left: 0,
            right: 0,
            height: 40,
            background: `linear-gradient(${COLOR.panel}, transparent)`,
            opacity: scroll > 4 ? 1 : 0,
            pointerEvents: "none",
          }}
        />
      </div>
    </div>
  );
};

export const StackView: React.FC<{
  sim: Sim;
  frame: number;
  anchorX?: number;
  baseY?: number;
}> = ({ sim, frame, anchorX = 716, baseY = 250 }) => {
  const camD = cameraDepth(sim.cam, frame);

  const visible = sim.frames.filter(
    (f) => frame >= f.bornAt - 6 && (f.returnedAt === null || frame < f.returnedAt + 18),
  );

  const live = sim.frames
    .filter((f) => frame >= f.bornAt && (f.returnedAt === null || frame < f.returnedAt))
    .sort((a, b) => a.depth - b.depth);

  const left = (depth: number) => anchorX + (depth - camD) * (FRAME_W + FRAME_GAP);
  const top = (depth: number) => baseY + (depth - camD) * STAIR_Y;

  // Connector start = the parent's "/call" message (bottom of its transcript).
  const callOrigin = (child: SimFrame): { x: number; y: number } | null => {
    const parent = sim.frames.find((p) => p.calls.some((c) => c.childId === child.id));
    if (!parent) return null;
    const { items, scroll } = frameItems(parent, frame);
    const idx = items.findIndex((it) => it.kind === "call" && it.childId === child.id);
    const x = left(parent.depth) + FRAME_W;
    if (idx < 0) return { x, y: top(parent.depth) + HEADER_H / 2 };
    let before = 0;
    for (let i = 0; i < idx; i++) before += items[i].h + BLOCK_GAP;
    const center = FRAME_PAD - scroll + before + items[idx].h / 2;
    const clamped = Math.max(10, Math.min(BODY_MAX_H - 10, center));
    return { x, y: top(parent.depth) + HEADER_H + clamped };
  };

  return (
    <AbsoluteFill>
      {/* breadcrumb of the live call chain */}
      <div
        style={{
          position: "absolute",
          top: 150,
          left: 0,
          right: 0,
          display: "flex",
          justifyContent: "center",
          gap: 8,
          alignItems: "center",
          flexWrap: "wrap",
          padding: "0 80px",
        }}
      >
        {live.map((f, i) => (
          <React.Fragment key={f.id}>
            {i > 0 ? (
              <span style={{ color: COLOR.textFaint, fontFamily: MONO, fontSize: 16 }}>▸</span>
            ) : null}
            <span
              style={{
                fontFamily: MONO,
                fontSize: 16,
                color: i === live.length - 1 ? COLOR.text : COLOR.textDim,
                background:
                  i === live.length - 1 ? `${f.depth === 0 ? COLOR.user : COLOR.call}22` : "transparent",
                border:
                  i === live.length - 1
                    ? `1px solid ${(f.depth === 0 ? COLOR.user : COLOR.call)}55`
                    : "1px solid transparent",
                borderRadius: 6,
                padding: "3px 9px",
              }}
            >
              {f.label}
            </span>
          </React.Fragment>
        ))}
      </div>

      {/* connectors: parent's /call message -> child's header */}
      <svg width={WIDTH} height={1080} style={{ position: "absolute", inset: 0, pointerEvents: "none" }}>
        {visible
          .filter((f) => f.depth > 0)
          .map((f) => {
            const origin = callOrigin(f);
            if (!origin) return null;
            const x1 = origin.x;
            const y1 = origin.y;
            const x2 = left(f.depth);
            const y2 = top(f.depth) + HEADER_H / 2;
            const on = ramp(frame, f.bornAt, 16);
            return (
              <g key={f.id} opacity={on * 0.85}>
                <path
                  d={`M ${x1} ${y1} C ${x1 + 40} ${y1}, ${x2 - 40} ${y2}, ${x2} ${y2}`}
                  stroke={COLOR.call}
                  strokeWidth={2}
                  fill="none"
                  strokeDasharray="2 5"
                  strokeLinecap="round"
                />
                <circle cx={x1} cy={y1} r={3} fill={COLOR.call} />
                <circle cx={x2} cy={y2} r={3.5} fill={COLOR.call} />
              </g>
            );
          })}
      </svg>

      {/* frames */}
      {visible.map((f) => {
        const dist = camD - f.depth;
        return (
          <div key={f.id} style={{ position: "absolute", left: left(f.depth), top: top(f.depth) }}>
            <FramePanel f={f} frame={frame} active={Math.abs(dist) < 0.5} dist={Math.max(0, dist)} />
          </div>
        );
      })}

      {/* left fade so receding parents dissolve off-edge */}
      <div
        style={{
          position: "absolute",
          inset: 0,
          pointerEvents: "none",
          background: `linear-gradient(90deg, ${COLOR.bg0} 0%, transparent 14%)`,
        }}
      />
    </AbsoluteFill>
  );
};
