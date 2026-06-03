// A tiny deterministic simulator that replays a list of timed call-stack
// operations (turn / call / return) into per-frame geometry. Used by the
// "with callstack" scene and the deep-interaction scene.

import { Easing, interpolate } from "remotion";

export type Role = "user" | "agent";

export type Op =
  | { t: number; kind: "turn"; role: Role }
  | { t: number; kind: "call"; id: string; label: string }
  | { t: number; kind: "return"; result: string };

export type SimBlock = {
  key: string;
  role: Role;
  h: number;
  bullets: number;
  seed: number;
  bornAt: number;
};

export type SimResult = { key: string; label: string; bornAt: number };
export type SimCall = { key: string; label: string; childId: string; bornAt: number };

export type SimFrame = {
  id: string;
  label: string;
  depth: number;
  bornAt: number;
  inherited: number;
  blocks: SimBlock[];
  calls: SimCall[];
  results: SimResult[];
  returnedAt: number | null;
};

export type Sim = {
  frames: SimFrame[];
  cam: { t: number; depth: number }[];
};

// Deterministic 0..1 hash (Math.random is forbidden in Remotion — it would
// flicker every frame). Stable per integer seed.
export const hash01 = (n: number): number => {
  const x = Math.sin(n * 127.1 + 311.7) * 43758.5453;
  return x - Math.floor(x);
};

// An agent turn is a multi-point response (1–6 bullets, more tokens → taller);
// a user turn is a short single message.
export function blockShape(role: Role, seed: number): { h: number; bullets: number } {
  if (role === "agent") {
    const bullets = 1 + Math.floor(hash01(seed) * 6); // 1..6
    return { bullets, h: 22 + bullets * 16 };
  }
  return { bullets: 0, h: 30 + Math.floor(hash01(seed + 9) * 12) };
}

// Delay before a returned child's result pill lands in the parent.
export const RETURN_DELAY = 16;

export function simulate(ops: Op[], rootLabel = "main session"): Sim {
  const root: SimFrame = {
    id: "root",
    label: rootLabel,
    depth: 0,
    bornAt: 0,
    inherited: 0,
    blocks: [],
    calls: [],
    results: [],
    returnedAt: null,
  };
  const frames: SimFrame[] = [root];
  const stack: SimFrame[] = [root];
  const cam: { t: number; depth: number }[] = [{ t: 0, depth: 0 }];
  let counter = 0;

  for (const op of ops) {
    const top = stack[stack.length - 1];
    if (op.kind === "turn") {
      counter += 1;
      const shape = blockShape(op.role, counter);
      top.blocks.push({
        key: `b${counter}`,
        role: op.role,
        h: shape.h,
        bullets: shape.bullets,
        seed: counter,
        bornAt: op.t,
      });
    } else if (op.kind === "call") {
      const f: SimFrame = {
        id: op.id,
        label: op.label,
        depth: top.depth + 1,
        bornAt: op.t,
        inherited:
          top.inherited + top.blocks.length + top.calls.length + top.results.length,
        blocks: [],
        calls: [],
        results: [],
        returnedAt: null,
      };
      // a "/call …" message appears at the bottom of the parent's transcript.
      counter += 1;
      top.calls.push({ key: `c${counter}`, label: op.label, childId: op.id, bornAt: op.t });
      frames.push(f);
      stack.push(f);
      cam.push({ t: op.t, depth: f.depth });
    } else {
      // return
      const done = stack.pop();
      if (done) done.returnedAt = op.t;
      const parent = stack[stack.length - 1];
      counter += 1;
      parent.results.push({
        key: `r${counter}`,
        label: op.result,
        bornAt: op.t + RETURN_DELAY,
      });
      cam.push({ t: op.t, depth: parent.depth });
    }
  }
  return { frames, cam };
}

const CAM_EASE = Easing.bezier(0.5, 0, 0.2, 1);

// Smoothly follow the deepest live frame: ease toward each camera keyframe in
// sequence over `dur` frames.
export function cameraDepth(cam: { t: number; depth: number }[], frame: number, dur = 26): number {
  let d = cam[0].depth;
  for (let i = 1; i < cam.length; i++) {
    const p = interpolate(frame, [cam[i].t, cam[i].t + dur], [0, 1], {
      easing: CAM_EASE,
      extrapolateLeft: "clamp",
      extrapolateRight: "clamp",
    });
    d = d + (cam[i].depth - d) * p;
  }
  return d;
}
