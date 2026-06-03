import React from "react";
import { AbsoluteFill, Sequence, useCurrentFrame, interpolate, Easing } from "remotion";
import { Background, ramp, Caption } from "../ui";
import { COLOR, MONO, SANS } from "../theme";

const EASE_IN = Easing.in(Easing.cubic);

// A customer-support agent in a deeply nested authentication flow. verify-mfa
// asks the user, then *dynamically* calls validate-mfa-code → check-code-expiry
// to check the answer — twice (a wrong code, then the right one). The agent
// drives the control flow; callstack pushes/pops the frames deterministically.
type Frame = {
  id: string;
  label: string;
  depth: number;
  bornAt: number;
  returnAt: number | null;
  result: { text: string; ok: boolean } | null;
};

const FRAMES: Frame[] = [
  { id: "orch", label: "orchestrator", depth: 0, bornAt: 0, returnAt: null, result: null },
  { id: "auth", label: "authenticate-customer", depth: 1, bornAt: 10, returnAt: null, result: null },
  { id: "vmfa", label: "verify-mfa", depth: 2, bornAt: 34, returnAt: 596, result: { text: "MFA verified", ok: true } },
  { id: "val1", label: "validate-mfa-code", depth: 3, bornAt: 178, returnAt: 280, result: { text: "invalid code", ok: false } },
  { id: "chk1", label: "check-code-expiry", depth: 4, bornAt: 206, returnAt: 240, result: { text: "invalid code", ok: false } },
  { id: "val2", label: "validate-mfa-code", depth: 3, bornAt: 456, returnAt: 556, result: { text: "code valid", ok: true } },
  { id: "chk2", label: "check-code-expiry", depth: 4, bornAt: 484, returnAt: 518, result: { text: "code valid", ok: true } },
];

// Result pills that land in a caller frame when its child returns.
type Pill = { text: string; ok: boolean; at: number };
const PILLS: Record<string, Pill[]> = {
  val1: [{ text: "invalid code", ok: false, at: 252 }],
  vmfa: [
    { text: "invalid code", ok: false, at: 292 },
    { text: "code valid", ok: true, at: 568 },
  ],
  val2: [{ text: "code valid", ok: true, at: 530 }],
  auth: [{ text: "MFA verified", ok: true, at: 608 }],
};

type Line = { kind: "prompt" | "user" | "sys"; text: string; start: number; cps: number; color?: string };
const LINES: Line[] = [
  { kind: "prompt", text: "Enter the 6-digit MFA code:", start: 66, cps: 0.5 },
  { kind: "user", text: "000000", start: 132, cps: 0.45 },
  { kind: "sys", text: "✕ Code rejected.", start: 290, cps: 0.5, color: COLOR.danger },
  { kind: "prompt", text: "That code was incorrect. Re-enter:", start: 334, cps: 0.5 },
  { kind: "user", text: "847291", start: 414, cps: 0.45 },
  { kind: "sys", text: "✓ Code accepted.", start: 540, cps: 0.5, color: COLOR.ok },
];

const ROW_X = 96;
const ROW_TOP = 250;
const ROW_GAP = 100;
const ROW_W = 470;
const TERM_X = 1146;
const TERM_Y = 286;
const TERM_W = 680;

// Ask windows: verify-mfa is awaiting the user (arrow to terminal is live).
const askActive = (f: number): boolean => (f >= 60 && f < 172) || (f >= 320 && f < 452);

const rowLeft = (depth: number): number => ROW_X + depth * 46;
const rowTop = (depth: number): number => ROW_TOP + depth * ROW_GAP;
const rowWidth = (depth: number): number => ROW_W - depth * 14;

const FramePanel: React.FC<{ f: Frame; frame: number; deepest: number }> = ({ f, frame, deepest }) => {
  const appear = ramp(frame, f.bornAt, 14);
  if (appear <= 0) return null;
  const returned = f.returnAt !== null && frame >= f.returnAt;
  const exit =
    f.returnAt === null
      ? 0
      : interpolate(frame, [f.returnAt, f.returnAt + 18], [0, 1], {
          easing: EASE_IN,
          extrapolateLeft: "clamp",
          extrapolateRight: "clamp",
        });
  if (exit >= 1) return null; // popped — frees the depth slot

  const live = frame >= f.bornAt && (f.returnAt === null || frame < f.returnAt);
  const awaiting = f.id === "vmfa" && askActive(frame);
  const active = live && f.depth === deepest && !awaiting;
  const frozen = live && f.depth < deepest;

  const accent = f.depth === 0 ? COLOR.user : returned ? (f.result?.ok ? COLOR.ok : COLOR.danger) : awaiting ? COLOR.call : active ? COLOR.agent : COLOR.textDim;
  const pulse = awaiting ? 0.5 + 0.5 * Math.sin(frame / 7) : 1;
  const pills = (PILLS[f.id] ?? []).filter((p) => frame >= p.at);

  return (
    <div
      style={{
        position: "absolute",
        left: rowLeft(f.depth),
        top: rowTop(f.depth),
        width: rowWidth(f.depth),
        opacity: appear * (1 - exit),
        transform: `translate(${interpolate(appear, [0, 1], [-16, 0])}px, ${-exit * 26}px)`,
      }}
    >
      <div
        style={{
          background: COLOR.panel,
          border: `1px solid ${awaiting ? COLOR.call : active ? COLOR.agent + "88" : returned ? accent + "88" : COLOR.panelEdge}`,
          borderLeft: `3px solid ${accent}`,
          borderRadius: 10,
          padding: "11px 14px",
          display: "flex",
          alignItems: "center",
          gap: 10,
          boxShadow: awaiting ? `0 0 ${18 * pulse}px ${COLOR.call}55` : active ? `0 0 16px ${COLOR.agent}33` : "none",
        }}
      >
        <span style={{ fontFamily: MONO, fontSize: 12, color: COLOR.textFaint }}>
          {f.depth === 0 ? "ROOT" : `${f.depth}`}
        </span>
        <span style={{ fontFamily: MONO, fontSize: 17, fontWeight: 600, color: COLOR.text, whiteSpace: "nowrap" }}>
          {f.depth === 0 ? f.label : `/call ${f.label}`}
        </span>
        <span style={{ marginLeft: "auto", fontFamily: MONO, fontSize: 13, whiteSpace: "nowrap", flexShrink: 0 }}>
          {returned ? (
            <span style={{ color: accent }}>↩ {f.result?.text}</span>
          ) : awaiting ? (
            <span style={{ color: COLOR.call, opacity: pulse }}>⏳ awaiting user</span>
          ) : active ? (
            <span style={{ color: COLOR.agent }}>running…</span>
          ) : frozen ? (
            <span style={{ color: COLOR.textFaint }}>⏸ frozen</span>
          ) : null}
        </span>
      </div>
      {/* result pills landing in this caller */}
      {pills.length > 0 ? (
        <div style={{ display: "flex", gap: 8, marginTop: 6, marginLeft: 16 }}>
          {pills.map((p, i) => (
            <span
              key={i}
              style={{
                fontFamily: MONO,
                fontSize: 13,
                color: p.ok ? COLOR.ok : COLOR.danger,
                background: p.ok ? "rgba(45,212,167,0.14)" : COLOR.dangerSoft,
                border: `1px solid ${p.ok ? COLOR.ok : COLOR.danger}55`,
                borderRadius: 6,
                padding: "2px 9px",
                opacity: ramp(frame, p.at, 12),
                transform: `translateY(${interpolate(ramp(frame, p.at, 12), [0, 1], [-8, 0])}px)`,
              }}
            >
              ↳ {p.ok ? "✓" : "✕"} {p.text}
            </span>
          ))}
        </div>
      ) : null}
    </div>
  );
};

const tw = (line: Line, frame: number): string => {
  const n = Math.floor((frame - line.start) * line.cps);
  if (n <= 0) return "";
  return line.text.slice(0, n);
};

export const S7_DeepInteraction: React.FC = () => {
  const frame = useCurrentFrame();
  const head = ramp(frame, 6, 16);

  const liveFrames = FRAMES.filter((f) => frame >= f.bornAt && (f.returnAt === null || frame < f.returnAt));
  const deepest = liveFrames.reduce((m, f) => Math.max(m, f.depth), 0);

  // verify-mfa frame geometry (arrow source during asks).
  const vmfa = FRAMES[2];
  const vmfaRight = rowLeft(vmfa.depth) + rowWidth(vmfa.depth);
  const vmfaMid = rowTop(vmfa.depth) + 24;
  const yieldOn = askActive(frame) ? 0.45 + 0.55 * Math.sin(frame / 6) : 0;

  return (
    <AbsoluteFill>
      <Background tint="rgba(242,169,59,0.10)" />

      <div style={{ position: "absolute", top: 74, left: 0, right: 0, textAlign: "center", opacity: head }}>
        <div style={{ fontFamily: SANS, fontSize: 40, fontWeight: 700, color: COLOR.text }}>
          Interaction at <span style={{ color: COLOR.call }}>any depth</span>
        </div>
        <div style={{ fontFamily: MONO, fontSize: 18, color: COLOR.textDim, marginTop: 8 }}>
          the agent decides to call deeper — callstack pushes &amp; pops each frame for it
        </div>
      </div>

      {/* connectors between live adjacent frames + the yield arrow */}
      <svg width={1920} height={1080} style={{ position: "absolute", inset: 0, pointerEvents: "none" }}>
        {FRAMES.filter((f) => f.depth > 0).map((f) => {
          const parent = FRAMES.find((p) => p.depth === f.depth - 1 && p.bornAt <= f.bornAt);
          if (!parent) return null;
          const on =
            ramp(frame, f.bornAt, 14) *
            (f.returnAt === null ? 1 : interpolate(frame, [f.returnAt, f.returnAt + 16], [1, 0], { extrapolateLeft: "clamp", extrapolateRight: "clamp" }));
          if (on <= 0) return null;
          const px = rowLeft(parent.depth) + 16;
          const py = rowTop(parent.depth) + 48;
          const cy = rowTop(f.depth) + 24;
          return (
            <path
              key={f.id}
              d={`M ${px} ${py} L ${px} ${cy} L ${rowLeft(f.depth)} ${cy}`}
              stroke={COLOR.textFaint}
              strokeWidth={1.5}
              fill="none"
              opacity={on * 0.7}
            />
          );
        })}
        {yieldOn > 0 ? (
          <g opacity={yieldOn}>
            <path
              d={`M ${vmfaRight} ${vmfaMid} C ${vmfaRight + 120} ${vmfaMid}, ${TERM_X - 120} ${TERM_Y + 150}, ${TERM_X - 12} ${TERM_Y + 150}`}
              stroke={COLOR.call}
              strokeWidth={2.5}
              strokeDasharray="3 6"
              fill="none"
            />
            <circle cx={TERM_X - 12} cy={TERM_Y + 150} r={5} fill={COLOR.call} />
          </g>
        ) : null}
      </svg>

      {askActive(frame) ? (
        <div
          style={{
            position: "absolute",
            left: vmfaRight + 40,
            top: vmfaMid - 86,
            width: 320,
            fontFamily: MONO,
            fontSize: 14,
            color: COLOR.call,
            lineHeight: 1.4,
            opacity: yieldOn,
          }}
        >
          verify-mfa yields to the user
          <div style={{ color: COLOR.textFaint }}>(it owns this conversation)</div>
        </div>
      ) : null}

      {/* the dynamic call stack */}
      {FRAMES.map((f) => (
        <FramePanel key={f.id} f={f} frame={frame} deepest={deepest} />
      ))}

      {/* user terminal */}
      <div style={{ position: "absolute", left: TERM_X, top: TERM_Y, width: TERM_W, opacity: head }}>
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
          <span style={{ fontFamily: MONO, fontSize: 14, color: COLOR.textDim, marginLeft: 8 }}>
            user terminal
          </span>
        </div>
        <div
          style={{
            background: "#0a0d15",
            border: `1px solid ${COLOR.panelEdge}`,
            borderTop: "none",
            borderRadius: "0 0 12px 12px",
            padding: 22,
            minHeight: 300,
            fontFamily: MONO,
            fontSize: 19,
            lineHeight: 1.75,
            display: "flex",
            flexDirection: "column",
            gap: 4,
          }}
        >
          {LINES.map((l) => {
            if (frame < l.start) return null;
            const text = tw(l, frame);
            const done = text.length >= l.text.length;
            const cursor = !done && Math.floor(frame / 8) % 2 === 0 ? "▌" : "";
            if (l.kind === "user") {
              return (
                <div key={l.start} style={{ color: COLOR.user }}>
                  <span style={{ color: COLOR.textFaint }}>$ </span>
                  {text}
                  {cursor}
                </div>
              );
            }
            if (l.kind === "sys") {
              return (
                <div key={l.start} style={{ color: l.color }}>
                  {text}
                  {cursor}
                </div>
              );
            }
            return (
              <div key={l.start} style={{ color: COLOR.text }}>
                <span style={{ color: COLOR.call }}>🔒 </span>
                {text}
                {cursor}
              </div>
            );
          })}
        </div>
      </div>

      <Sequence from={430} durationInFrames={210} layout="none">
        <Caption total={210} accent={COLOR.ok}>
          The agent generated this flow on the fly — callstack just made every push and pop reliable.
        </Caption>
      </Sequence>
    </AbsoluteFill>
  );
};
