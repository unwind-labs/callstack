// Shared design tokens for the callstack explainer.

export const FPS = 30;
export const WIDTH = 1920;
export const HEIGHT = 1080;

export const SANS =
  "'Inter', system-ui, -apple-system, 'Segoe UI', Helvetica, Arial, sans-serif";
export const MONO =
  "'SF Mono', 'JetBrains Mono', ui-monospace, Menlo, Consolas, monospace";

export const COLOR = {
  bg0: "#070A12",
  bg1: "#0C111E",
  panel: "#121826",
  panelEdge: "rgba(255,255,255,0.07)",
  grid: "rgba(120,150,210,0.05)",

  text: "#E8EEF7",
  textDim: "#8A98AE",
  textFaint: "#566179",

  // Conversation turns
  user: "#4D8DF6", // blue — user turns
  userSoft: "rgba(77,141,246,0.16)",
  agent: "#2DD4A7", // teal/green — agent turns
  agentSoft: "rgba(45,212,167,0.15)",

  // Semantics
  call: "#F2A93B", // amber — /call
  callSoft: "rgba(242,169,59,0.16)",
  result: "#B98BFB", // violet — returned result pills
  resultSoft: "rgba(185,139,251,0.18)",
  danger: "#F2545B", // red — losing track / failure
  dangerSoft: "rgba(242,84,91,0.16)",
  ok: "#2DD4A7",
} as const;

// Conversation-block geometry (call-stack scenes).
export const BLOCK_W = 296;
export const BLOCK_GAP = 9;
export const FRAME_W = 344;
export const FRAME_GAP = 58;
export const STAIR_Y = 30;
