#!/usr/bin/env node

import fs from "node:fs";
import path from "node:path";

const root = path.resolve(process.argv[2] ?? ".");
const charactersDocument = JSON.parse(fs.readFileSync(path.join(root, "production", "characters.json"), "utf8"));
const storyboard = JSON.parse(fs.readFileSync(path.join(root, "STORYBOARD_VIDEO.json"), "utf8"));
const characters = charactersDocument.characters;
const style = charactersDocument.style;
const globalNegative = charactersDocument.negativePrompt;
const modelProfile = JSON.parse(fs.readFileSync(path.join(root, "production", "comfy-model-profile.json"), "utf8"));
const videoSampleIds = new Set(modelProfile.gates.videoSampleIds);
const keyframeRevisions = JSON.parse(
  fs.readFileSync(path.join(root, "production", "keyframe-revisions.json"), "utf8"),
).revisions;

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

const coverDrafts = [
  {
    id: "cover-draft-horizontal",
    stage: "cover-drafts",
    kind: "qwen-t2i",
    seed: 415001,
    width: 1328,
    height: 992,
    targetWidth: 1600,
    targetHeight: 1200,
    prompt: `No-text 4:3 horizontal comic cover draft. Exactly two people: Chen Yan dominates the foreground in his fixed white-and-navy school uniform and black backpack; the riverside girl is a smaller distant secondary figure under rain beside a safe riverside railing. Blue-black storm, sodium-orange streetlight and one restrained lightning rim. Deliberate clean negative space in the lower-left for local typography. Non-graphic suspense, no self-harm pose. Style: ${style}.`,
    negativePrompt: globalNegative,
    references: [],
    output: "publishing/source/cover-draft-horizontal.png",
    maxAttempts: 3,
    timeoutSeconds: 3600,
  },
  {
    id: "cover-draft-vertical",
    stage: "cover-drafts",
    kind: "qwen-t2i",
    seed: 415002,
    width: 768,
    height: 1024,
    targetWidth: 1200,
    targetHeight: 1600,
    prompt: `No-text 3:4 vertical comic cover draft. Exactly two people: Chen Yan fills the upper-right foreground in his fixed white-and-navy school uniform and black backpack; the riverside girl is a smaller distant secondary figure below, safely inside a rain-soaked riverside railing. Blue-black storm, sodium-orange streetlight and one restrained lightning rim. Deliberate clean negative space in the lower-left for local typography. Non-graphic suspense, no self-harm pose. Style: ${style}.`,
    negativePrompt: globalNegative,
    references: [],
    output: "publishing/source/cover-draft-vertical.png",
    maxAttempts: 3,
    timeoutSeconds: 3600,
  },
];

const covers = [
  {
    id: "cover-art-horizontal",
    stage: "covers",
    kind: "qwen-edit",
    seed: 425001,
    targetWidth: 1600,
    targetHeight: 1200,
    prompt: `Use picture 1 only for the 4:3 composition and negative space. Replace its foreground boy with the exact Chen Yan identity from picture 2 and its distant girl with the exact riverside-girl identity from picture 3. Preserve their immutable faces, ages, hairstyles and school uniforms. Keep the rain-blue and sodium-orange cinematic Chinese webtoon look. Remove all text, letters, numbers, logos, watermarks and guide marks.`,
    negativePrompt: globalNegative,
    references: [
      "publishing/source/cover-draft-horizontal.png",
      characters["chen-yan"].output,
      characters["riverside-girl"].output,
    ],
    output: "publishing/source/cover-art-horizontal.png",
    maxAttempts: 3,
    timeoutSeconds: 3600,
  },
  {
    id: "cover-art-vertical",
    stage: "covers",
    kind: "qwen-edit",
    seed: 425002,
    targetWidth: 1200,
    targetHeight: 1600,
    prompt: `Use picture 1 only for the 3:4 vertical composition and negative space. Replace its foreground boy with the exact Chen Yan identity from picture 2 and its distant girl with the exact riverside-girl identity from picture 3. Preserve their immutable faces, ages, hairstyles and school uniforms. Keep the rain-blue and sodium-orange cinematic Chinese webtoon look. Remove all text, letters, numbers, logos, watermarks and guide marks.`,
    negativePrompt: globalNegative,
    references: [
      "publishing/source/cover-draft-vertical.png",
      characters["chen-yan"].output,
      characters["riverside-girl"].output,
    ],
    output: "publishing/source/cover-art-vertical.png",
    maxAttempts: 3,
    timeoutSeconds: 3600,
  },
];

const keyframes = storyboard.map((scene, index) => {
  const allowed = scene.participants.allowed;
  const identityReferences = allowed.map((id) => characters[id]?.output);
  if (identityReferences.some((value) => !value)) {
    throw new Error(`${scene.id} references an unknown character: ${allowed.join(", ")}`);
  }
  const revision = keyframeRevisions[scene.id];
  const cleanup = revision?.mode === "cleanup";
  const references = cleanup
    ? [revision.source ?? scene.sourceImage, ...identityReferences].slice(0, 3)
    : identityReferences;
  const roles = cleanup
    ? `picture 1 is the rejected first pass; ${allowed
        .map((id, referenceIndex) => `picture ${referenceIndex + 2} is the approved ${id} identity`)
        .join("; ")}`
    : allowed.map((id, referenceIndex) => `reference image ${referenceIndex + 1} is ${id}`).join("; ");
  const prompt = cleanup
    ? [
        `Conservative second-pass cleanup: ${roles}.`,
        `Preserve picture 1's pose, camera, lighting, composition, environment and intended action exactly.`,
        `Preserve the approved faces, ages, hairstyles and clothing from the identity pictures.`,
        `Remove only these named defects: ${(revision.defects ?? []).join("; ")}.`,
        `Do not redesign, add people, change expression, crop, or add text.`,
      ].join(" ")
    : [
        `Edit the supplied identity references into one new cinematic 16:9 story frame: ${roles}.`,
        `Change only the setting, pose, action, expression and camera needed for this scene; keep the referenced identity and clothing.`,
        `Show exactly ${scene.participants.count} visible person(s), and no one else.`,
        scene.description,
        `Shot: ${scene.shot}.`,
        `Preserve every referenced face, age, hair growth pattern, recognition mark, body type and wardrobe from the identity anchor.`,
        `Use the recurring location geometry and weather from CHARACTERS.md. Style: ${style}.`,
        `Keep every hand, limb, phone, railing, shoe and support point physically plausible.`,
        `All screens, papers, uniforms and signs must remain completely blank; exact text will be added locally.`,
      ].join(" ");
  return {
    id: `keyframe-${scene.id}`,
    stage: "keyframes",
    kind: "qwen-edit",
    seed: 420001 + index + Number(revision?.seedOffset ?? 0),
    prompt,
    negativePrompt: `${globalNegative}, wrong participant count, extra lookalike protagonist, impossible railing, floating body, reversed phone, transparent or sexualized school uniform`,
    references,
    output: scene.sourceImage,
    sourceSceneId: scene.sourceSceneId,
    highRisk: scene.highRisk,
    revision: cleanup ? revision : null,
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
    `真实连续国漫镜头，不是静态推拉、平移或定格。`,
    scene.motionTarget,
    `只出现 ${scene.participants.count} 人（${scene.participants.allowed.join(", ")}），锁定脸、年龄、发型、服装和首帧构图。`,
    `动作、雨、头发和衣物符合实时物理；全程保持精致半写实国漫画风，不新增物体或文字。`,
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
  representativeSample: videoSampleIds.has(scene.id),
  maxAttempts: 3,
  timeoutSeconds: 7200,
}));

const jobs = [...anchors, ...coverDrafts, ...covers, ...keyframes, ...videos];
const ids = new Set(jobs.map((job) => job.id));
const outputs = new Set(jobs.map((job) => job.output));
if (ids.size !== jobs.length) throw new Error("Generation queue contains duplicate job ids");
if (outputs.size !== jobs.length) throw new Error("Generation queue contains duplicate output paths");

const document = {
  version: 1,
  generatedFrom: [
    "production/characters.json",
    "production/comfy-model-profile.json",
    "production/keyframe-revisions.json",
    "STORYBOARD_VIDEO.json",
  ],
  policy: "production/quality-policy.json",
  counts: {
    anchors: anchors.length,
    coverDrafts: coverDrafts.length,
    covers: covers.length,
    keyframes: keyframes.length,
    videoSample: videos.filter((job) => job.representativeSample).length,
    videos: videos.length,
  },
  jobs,
};
const output = path.join(root, "production", "generation-queue.json");
fs.writeFileSync(output, `${JSON.stringify(document, null, 2)}\n`);
const promptDir = path.join(root, "assets", "generated", "prompts");
fs.mkdirSync(promptDir, { recursive: true });
const sheetMap = {
  version: 1,
  mode: "standalone-singles-only",
  storyboard: "STORYBOARD_VIDEO.json",
  jobs: keyframes.map((job) => ({
    jobId: job.id,
    delivery: "single",
    sceneIds: [job.id.replace(/^keyframe-/, "")],
    output: job.output,
    references: job.references,
  })),
  coverage: {
    expectedScenes: storyboard.length,
    mappedScenes: keyframes.length,
    duplicates: 0,
    missing: 0,
  },
};
fs.writeFileSync(path.join(promptDir, "SHEET_MAP.json"), `${JSON.stringify(sheetMap, null, 2)}\n`);
console.log(JSON.stringify({ output, counts: document.counts, total: jobs.length }, null, 2));
