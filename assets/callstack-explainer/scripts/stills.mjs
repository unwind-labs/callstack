// Bundle once, render a batch of stills for visual QA.
import { bundle } from "@remotion/bundler";
import { selectComposition, renderStill } from "@remotion/renderer";
import path from "node:path";

const frames = process.argv.slice(2).map(Number);
const list = frames.length ? frames : [90, 487, 973, 1063, 1454, 1674, 1814, 1960, 2221, 2381, 2597];

const serveUrl = await bundle({ entryPoint: path.resolve("src/index.ts") });
const composition = await selectComposition({ serveUrl, id: "Explainer" });

for (const frame of list) {
  const output = `/tmp/exp_${String(frame).padStart(4, "0")}.png`;
  await renderStill({ composition, serveUrl, output, frame, scale: 0.5, overwrite: true });
  console.log("rendered", output);
}
process.exit(0);
