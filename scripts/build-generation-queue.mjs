#!/usr/bin/env node

import fs from "node:fs";
import path from "node:path";

const root = path.resolve(process.argv[2] ?? ".");
const charactersDocument = JSON.parse(fs.readFileSync(path.join(root, "production", "characters.json"), "utf8"));
const storyboard = JSON.parse(fs.readFileSync(path.join(root, "STORYBOARD_VIDEO.json"), "utf8"));
const characters = charactersDocument.characters;
const style = charactersDocument.style;
const globalNegative = charactersDocument.negativePrompt;

const anchors = Object.entries(characters).map(([id, character]) => ({
  id: `anchor-${id}`,
  stage: "anchors",
  kind: "qwen-t2i",
  seed: character.seed,
  width: 1328,
  height: 768,
  prompt: `${character.prompt} Style: ${style}. Generated surfaces contain no text.`,
  negativePrompt: globalNegative,
  references: [],
  output: character.output,
  maxAttempts: 3,
  timeoutSeconds: 3600,
}));

const keyframes = storyboard.map((scene, index) => {
  const allowed = scene.participants.allowed;
  const references = allowed.map((id) => characters[id]?.output);
  if (references.some((value) => !value)) {
    throw new Error(`${scene.id} references an unknown character: ${allowed.join(", ")}`);
  }
  const roles = allowed.map((id, referenceIndex) => `reference image ${referenceIndex + 1} is ${id}`).join("; ");
  return {
    id: `keyframe-${scene.id}`,
    stage: "keyframes",
    kind: "qwen-edit",
    seed: 420001 + index,
    prompt: [
      `Use the supplied identity references exactly: ${roles}.`,
      `Create one new cinematic 16:9 story frame with exactly ${scene.participants.count} visible person(s), and no one else.`,
      scene.description,
      `Shot: ${scene.shot}.`,
      `Preserve every referenced face, age, hair growth pattern, recognition mark, body type and wardrobe from the identity anchor.`,
      `Use the recurring location geometry and weather from CHARACTERS.md. Style: ${style}.`,
      `Keep every hand, limb, phone, railing, shoe and support point physically plausible.`,
      `All screens, papers, uniforms and signs must remain completely blank; exact text will be added locally.`,
    ].join(" "),
    negativePrompt: `${globalNegative}, wrong participant count, extra lookalike protagonist, impossible railing, floating body, reversed phone, transparent or sexualized school uniform`,
    references,
    output: scene.sourceImage,
    sourceSceneId: scene.sourceSceneId,
    highRisk: scene.highRisk,
    maxAttempts: 3,
    timeoutSeconds: 3600,
  };
});

const videos = storyboard.map((scene, index) => ({
  id: `video-${scene.id}`,
  stage: "videos",
  kind: "wan-i2v",
  seed: 430001 + index,
  prompt: [
    `Animate the supplied frame into a continuous cinematic shot; do not merely zoom, pan or hold the still image.`,
    scene.motionTarget,
    `Keep exactly ${scene.participants.count} visible person(s): ${scene.participants.allowed.join(", ")}; no other person may appear.`,
    `Lock facial identity, age, hairstyle, recognition marks, body proportions, uniform and all first-frame geometry.`,
    `Natural real-time body physics, rain, cloth, hair, eye and environmental motion.`,
    `Preserve the polished Chinese webtoon line style and refined cel shading in every frame.`,
    `The final frame must remain a valid continuation of the first frame with no new object, text or identity drift.`,
  ].join(" "),
  negativePrompt: [
    "static frame, frozen image, Ken Burns movement, slideshow",
    "identity drift, face morph, age drift, wardrobe drift",
    "extra person, duplicate body, extra limb, fused hand, detached finger",
    "rubber motion, camera shake, micro jitter, edge warping, melting background",
    "new object, reversed phone, impossible railing or gravity",
    "letters, Chinese characters, numbers, subtitle, logo, watermark, red guide mark",
  ].join(", "),
  references: [scene.sourceImage],
  output: scene.asset,
  duration: scene.duration,
  width: 1280,
  height: 720,
  fps: 16,
  sourceCaptionIds: scene.sourceCaptionIds,
  highRisk: scene.highRisk,
  maxAttempts: 3,
  timeoutSeconds: 7200,
}));

const jobs = [...anchors, ...keyframes, ...videos];
const ids = new Set(jobs.map((job) => job.id));
const outputs = new Set(jobs.map((job) => job.output));
if (ids.size !== jobs.length) throw new Error("Generation queue contains duplicate job ids");
if (outputs.size !== jobs.length) throw new Error("Generation queue contains duplicate output paths");

const document = {
  version: 1,
  generatedFrom: ["production/characters.json", "STORYBOARD_VIDEO.json"],
  policy: "production/quality-policy.json",
  counts: { anchors: anchors.length, keyframes: keyframes.length, videos: videos.length },
  jobs,
};
const output = path.join(root, "production", "generation-queue.json");
fs.writeFileSync(output, `${JSON.stringify(document, null, 2)}\n`);
console.log(JSON.stringify({ output, counts: document.counts, total: jobs.length }, null, 2));

