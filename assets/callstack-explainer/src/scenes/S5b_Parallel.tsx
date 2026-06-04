import React from "react";
import { AbsoluteFill, useCurrentFrame, interpolate, Easing } from "remotion";
import { Background, TurnBlock, ramp } from "../ui";
import { COLOR, MONO, SANS, WIDTH, FRAME_W } from "../theme";

// Same feel as S5: the "current" session(s) stay centered on screen and glow;
// callers slide aside but stay visible. Parent centered alone → /call → three
// glowing children become the centered current set (parent slides left) → as
// each returns it vanishes and the remaining children re-center → finally only
// the parent remains, re-centered, glowing. Scene-local frames @30fps.
const EASE_IN = Easing.in(Easing.cubic);
const CAM_EASE = Easing.bezier(0.5, 0, 0.2, 1);

const FRAME_W2 = FRAME_W; // same session width as S5
const SLOT = FRAME_W + 44;
const FORK = 170;
const CENTER = WIDTH / 2;
const PARENT_TOP = 210;
const CHILD_TOP = 470;
const HEADER_H = 44;
const CALL_DY = HEADER_H + 14 + 30 + 9 + 54 + 9 + 16; // /call line center from panel top

// Group-centered left edges for n children, k-th of them.
const groupLeft = (n: number): number => CENTER - (n * FRAME_W + (n - 1) * 44) / 2;

// children left→right: api, auth, db. ret = return frame (more msgs ⇒ later).
const KIDS = [
  { file: "api.py", msgs: 2, ret: 280, lefts: [{ t: FORK, v: groupLeft(3) }] },
  {
    file: "auth.py",
    msgs: 4,
    ret: 360,
    lefts: [
      { t: FORK, v: groupLeft(3) + SLOT },
      { t: 280, v: groupLeft(2) },
      { t: 320, v: groupLeft(1) },
    ],
  },
  {
    file: "db.py",
    msgs: 3,
    ret: 320,
    lefts: [
      { t: FORK, v: groupLeft(3) + 2 * SLOT },
      { t: 280, v: groupLeft(2) + SLOT },
    ],
  },
];
// parent slides: centered → far left (3 kids) → in (2) → in (1) → centered.
const PARENT_LEFTS = [
  { t: 0, v: CENTER - FRAME_W / 2 },
  { t: FORK, v: groupLeft(3) - SLOT },
  { t: 280, v: groupLeft(2) - SLOT },
  { t: 320, v: groupLeft(1) - SLOT },
  { t: 360, v: CENTER - FRAME_W / 2 },
];
// parent result rows, in completion order
const RETURNS = [
  { file: "api.py", at: 292 },
  { file: "db.py", at: 332 },
  { file: "auth.py", at: 372 },
];

const childAppear = (i: number): number => FORK + i * 4;
const isLive = (k: (typeof KIDS)[number], i: number, frame: number): boolean =>
  frame >= childAppear(i) && frame < k.ret;

// Ease through a list of {t, v} keyframes (same follower idea as S5's camera).
function track(keys: { t: number; v: number }[], frame: number, dur = 22): number {
  let v = keys[0].v;
  for (let i = 1; i < keys.length; i++) {
    const p = interpolate(frame, [keys[i].t, keys[i].t + dur], [0, 1], {
      easing: CAM_EASE,
      extrapolateLeft: "clamp",
      extrapolateRight: "clamp",
    });
    v = v + (keys[i].v - v) * p;
  }
  return v;
}

const Header: React.FC<{ tag: string; accent: string; label: string }> = ({ tag, accent, label }) => (
  <div
    style={{
      height: HEADER_H,
      width: FRAME_W2,
      borderRadius: "12px 12px 0 0",
      background: `${accent}1f`,
      border: `1px solid ${accent}55`,
      display: "flex",
      alignItems: "center",
      gap: 8,
      padding: "0 13px",
      boxSizing: "border-box",
    }}
  >
    <span
      style={{
        fontFamily: MONO,
        fontSize: 11,
        color: accent,
        background: `${accent}22`,
        borderRadius: 5,
        padding: "2px 6px",
        fontWeight: 700,
      }}
    >
      {tag}
    </span>
    <span
      style={{
        fontFamily: SANS,
        fontSize: 16,
        fontWeight: 600,
        color: COLOR.text,
        whiteSpace: "nowrap",
        overflow: "hidden",
        textOverflow: "ellipsis",
      }}
    >
      {label}
    </span>
  </div>
);

const panelBody = (accent: string, glow: boolean): React.CSSProperties => ({
  background: COLOR.panel,
  border: `1px solid ${accent}${glow ? "66" : "33"}`,
  borderTop: "none",
  borderRadius: "0 0 12px 12px",
  padding: 13,
  boxSizing: "border-box",
  display: "flex",
  flexDirection: "column",
  gap: 8,
  width: FRAME_W2,
  boxShadow: glow ? `0 24px 70px -26px ${accent}aa` : "none",
});

const Inherited: React.FC = () => (
  <div
    style={{
      border: "1px dashed rgba(255,255,255,0.12)",
      borderRadius: 9,
      padding: "7px 9px",
      display: "flex",
      flexDirection: "column",
      gap: 4,
      background: "rgba(255,255,255,0.015)",
    }}
  >
    <div style={{ fontFamily: MONO, fontSize: 11, color: COLOR.textFaint }}>
      ⤴ inherited context · knows the correction
    </div>
    {[80, 58].map((w, k) => (
      <div
        key={k}
        style={{
          height: 4,
          borderRadius: 4,
          background: k % 2 === 0 ? "rgba(77,141,246,0.28)" : "rgba(45,212,167,0.28)",
          width: `${w}%`,
        }}
      />
    ))}
  </div>
);

const ResultRow: React.FC<{ appear: number; label: string }> = ({ appear, label }) => (
  <div style={{ height: 30 * appear, overflow: "hidden", opacity: appear }}>
    <div
      style={{
        height: 30,
        width: FRAME_W2,
        borderRadius: 9,
        background: COLOR.resultSoft,
        border: `1px solid ${COLOR.result}66`,
        display: "flex",
        alignItems: "center",
        gap: 8,
        padding: "0 12px",
        boxSizing: "border-box",
        fontFamily: MONO,
        fontSize: 13,
        color: COLOR.result,
      }}
    >
      <span style={{ fontWeight: 700 }}>↩</span>
      <span style={{ color: COLOR.text, opacity: 0.85 }}>{label}</span>
    </div>
  </div>
);

const Child: React.FC<{ i: number; frame: number; left: number; glow: boolean }> = ({
  i,
  frame,
  left,
  glow,
}) => {
  const kid = KIDS[i];
  const appear = ramp(frame, childAppear(i), 16);
  const exit = interpolate(frame, [kid.ret, kid.ret + 18], [0, 1], {
    easing: EASE_IN,
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });
  if (appear <= 0 || exit >= 1) return null;
  return (
    <div
      style={{
        position: "absolute",
        left,
        top: CHILD_TOP,
        width: FRAME_W2,
        opacity: appear * (1 - exit),
        transform: `translateY(${interpolate(appear, [0, 1], [18, 0]) - exit * 30}px) scale(${1 - exit * 0.06})`,
        transformOrigin: "top center",
      }}
    >
      <Header tag="/call" accent={COLOR.call} label={`fix ${kid.file}`} />
      <div style={panelBody(COLOR.call, glow)}>
        <Inherited />
        {Array.from({ length: kid.msgs }).map((_, j) => {
          const a = ramp(frame, 206 + j * 40, 12);
          return (
            <div key={j} style={{ height: 40 * a, overflow: "hidden" }}>
              <TurnBlock
                role={j % 2 === 0 ? "agent" : "user"}
                height={40}
                width={FRAME_W2 - 26}
                appear={a}
                bullets={j % 2 === 0 ? 1 : 0}
                seed={i * 7 + j}
              />
            </div>
          );
        })}
      </div>
    </div>
  );
};

export const S5b_Parallel: React.FC = () => {
  const frame = useCurrentFrame();
  const head = ramp(frame, 6, 16);

  const pHead = ramp(frame, 8, 16);
  const pT1 = ramp(frame, 14, 14);
  const pT2 = ramp(frame, 34, 14);
  const callLine = ramp(frame, 120, 14);
  const cont = ramp(frame, 400, 16);

  const parentLeft = track(PARENT_LEFTS, frame);
  const childLefts = KIDS.map((k) => track(k.lefts, frame));
  const anyLive = KIDS.some((k, i) => isLive(k, i, frame));
  const parentGlow = !anyLive;
  const callOX = parentLeft + FRAME_W2;
  const callOY = PARENT_TOP + CALL_DY;

  return (
    <AbsoluteFill>
      <Background tint="rgba(242,169,59,0.10)" />

      <div style={{ position: "absolute", top: 56, left: 0, right: 0, textAlign: "center", opacity: head }}>
        <div style={{ fontFamily: SANS, fontSize: 38, fontWeight: 700, color: COLOR.text }}>
          Parallel <span style={{ color: COLOR.call }}>/call</span>
        </div>
        <div style={{ fontFamily: MONO, fontSize: 17, color: COLOR.textDim, marginTop: 6 }}>
          one command fans out — each result returns to the parent
        </div>
      </div>

      {/* connectors from the /call point to each live child */}
      <svg width={WIDTH} height={1080} style={{ position: "absolute", inset: 0, pointerEvents: "none" }}>
        {KIDS.map((kid, i) => {
          if (!isLive(kid, i, frame)) return null;
          const cx = childLefts[i] + FRAME_W2 / 2;
          const on = ramp(frame, childAppear(i), 16);
          return (
            <g key={kid.file} opacity={on * 0.85}>
              <path
                d={`M ${callOX} ${callOY} C ${callOX + 50} ${callOY + 30}, ${cx} ${CHILD_TOP - 60}, ${cx} ${CHILD_TOP}`}
                stroke={COLOR.call}
                strokeWidth={2}
                fill="none"
                strokeDasharray="2 5"
                strokeLinecap="round"
              />
              <circle cx={callOX} cy={callOY} r={3} fill={COLOR.call} />
              <circle cx={cx} cy={CHILD_TOP} r={3.5} fill={COLOR.call} />
            </g>
          );
        })}
        {/* return pulses */}
        {KIDS.map((kid, i) => {
          const pulse = interpolate(frame, [kid.ret, kid.ret + 16], [0, 1], {
            extrapolateLeft: "clamp",
            extrapolateRight: "clamp",
          });
          if (pulse <= 0 || pulse >= 1) return null;
          const cx = childLefts[i] + FRAME_W2 / 2;
          return (
            <circle
              key={`p${kid.file}`}
              cx={cx + (callOX - cx) * pulse}
              cy={CHILD_TOP + (callOY - CHILD_TOP) * pulse}
              r={6}
              fill={COLOR.result}
            />
          );
        })}
      </svg>

      {/* parent session */}
      <div style={{ position: "absolute", left: parentLeft, top: PARENT_TOP, width: FRAME_W2, opacity: pHead }}>
        <Header tag="ROOT" accent={COLOR.user} label="main session" />
        <div style={panelBody(COLOR.user, parentGlow)}>
          <div style={{ height: 30 * pT1, overflow: "hidden" }}>
            <TurnBlock role="user" height={30} width={FRAME_W2 - 26} appear={pT1} />
          </div>
          <div style={{ height: 54 * pT2, overflow: "hidden" }}>
            <TurnBlock role="agent" height={54} width={FRAME_W2 - 26} appear={pT2} bullets={2} seed={2} />
          </div>
          <div style={{ height: 32 * callLine, overflow: "hidden", opacity: callLine }}>
            <div
              style={{
                height: 32,
                width: FRAME_W2,
                borderRadius: 9,
                background: COLOR.callSoft,
                border: `1px solid ${COLOR.call}66`,
                borderLeft: `3px solid ${COLOR.call}`,
                display: "flex",
                alignItems: "center",
                gap: 7,
                padding: "0 11px",
                boxSizing: "border-box",
                fontFamily: MONO,
                fontSize: 13,
                color: COLOR.call,
              }}
            >
              <span style={{ fontWeight: 700 }}>/call</span>
              <span style={{ color: COLOR.text, opacity: 0.85, whiteSpace: "nowrap" }}>
                fix each file · parallel
              </span>
            </div>
          </div>
          {RETURNS.map((r) => (
            <ResultRow key={r.file} appear={ramp(frame, r.at, 14)} label={`${r.file} fixed ✓`} />
          ))}
          <div style={{ height: 56 * cont, overflow: "hidden" }}>
            <TurnBlock role="agent" height={56} width={FRAME_W2 - 26} appear={cont} bullets={2} seed={9} />
          </div>
        </div>
      </div>

      {/* forked children */}
      {KIDS.map((kid, i) => (
        <Child key={kid.file} i={i} frame={frame} left={childLefts[i]} glow={isLive(kid, i, frame)} />
      ))}
    </AbsoluteFill>
  );
};
