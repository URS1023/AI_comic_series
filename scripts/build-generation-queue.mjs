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
const qualityPolicy = JSON.parse(fs.readFileSync(path.join(root, "production", "quality-policy.json"), "utf8"));
const locationsDocument = JSON.parse(fs.readFileSync(path.join(root, "production", "locations.json"), "utf8"));
const motionEndframes = JSON.parse(
  fs.readFileSync(path.join(root, "production", "motion-endframes.json"), "utf8"),
).scenes;
const videoSampleIds = new Set(modelProfile.gates.videoSampleIds);
const keyframeRevisions = JSON.parse(
  fs.readFileSync(path.join(root, "production", "keyframe-revisions.json"), "utf8"),
).revisions;

const identityAnchors = Object.entries(characters).map(([id, character]) => ({
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

const locationAnchors = Object.entries(locationsDocument.locations).map(([id, location]) => ({
  id: `anchor-location-${id}`,
  stage: "anchors",
  kind: "qwen-t2i",
  seed: location.seed,
  width: 1328,
  height: 768,
  prompt: `${location.prompt} Style: ${locationsDocument.style}. Generated surfaces contain no text.`,
  negativePrompt: locationsDocument.negativePrompt,
  references: [],
  output: location.output,
  anchorType: "location",
  maxAttempts: 3,
  timeoutSeconds: 3600,
}));

const anchors = [...identityAnchors, ...locationAnchors];

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
  const locationId = locationsDocument.sceneLocations[scene.id];
  const location = locationsDocument.locations[locationId];
  if (!location) throw new Error(`${scene.id} has no valid location anchor mapping`);
  const revision = keyframeRevisions[scene.id];
  const cleanup = revision?.mode === "cleanup";
  const references = cleanup
    ? [revision.source ?? scene.sourceImage, ...identityReferences].slice(0, 3)
    : [...identityReferences, location.output].slice(0, 3);
  const roles = cleanup
    ? `picture 1 is the rejected first pass; ${allowed
        .map((id, referenceIndex) => `picture ${referenceIndex + 2} is the approved ${id} identity`)
        .join("; ")}`
    : [
        ...allowed.map((id, referenceIndex) => `picture ${referenceIndex + 1} is ${id}`),
        `picture ${identityReferences.length + 1} is the approved ${locationId} location geometry`,
      ].join("; ");
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
        `Preserve the approved location geometry from the location picture: ${location.prompt} Style: ${style}.`,
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

const motionKeyframes = Object.entries(motionEndframes).map(([sceneId, specification], index) => {
  const scene = storyboard.find((candidate) => candidate.id === sceneId);
  if (!scene) throw new Error(`Motion end-frame references an unknown scene: ${sceneId}`);
  const identityReferences = scene.participants.allowed.map((id) => characters[id]?.output);
  if (identityReferences.some((value) => !value)) {
    throw new Error(`${scene.id} references an unknown character in motion end-frame`);
  }
  const identityRoles = scene.participants.allowed
    .map((id, referenceIndex) => `picture ${referenceIndex + 2} is the approved ${id} identity`)
    .join("; ");
  return {
    id: `motion-endframe-${scene.id}`,
    stage: "motion-keyframes",
    kind: "qwen-edit",
    seed: 440001 + index + Number(specification.seedOffset ?? 0),
    prompt: [
      `Create the reviewed ending frame for one continuous shot: picture 1 is its approved starting frame; ${identityRoles}.`,
      `Keep picture 1's exact location, camera axis, lens, weather, lighting, palette and wardrobe.`,
      `Advance only the intended physical action to this safe ending state: ${specification.endDescription}`,
      `Preserve every approved face, age, hair pattern, recognition mark and body type.`,
      `Show exactly ${scene.participants.count} visible person(s), with complete separated anatomy and physically correct support points.`,
      `Do not add text, UI, signs, logos, watermarks, people, props or a second panel. Style: ${style}.`,
    ].join(" "),
    negativePrompt: `${globalNegative}, wrong camera axis, discontinuous location, wrong participant count, extra lookalike, duplicated body, impossible contact, floating body, changed railing, injury, blood`,
    references: [scene.sourceImage, ...identityReferences].slice(0, 3),
    output: `assets/generated/endframes/${scene.id}.png`,
    sourceSceneId: scene.sourceSceneId,
    motionSceneId: scene.id,
    highRisk: true,
    maxAttempts: 3,
    timeoutSeconds: 3600,
  };
});

const endFrameByScene = new Map(motionKeyframes.map((job) => [job.motionSceneId, job.output]));

const videos = storyboard.map((scene, index) => ({
  id: `video-${scene.id}`,
  stage: "videos",
  kind: endFrameByScene.has(scene.id) ? "wan-flf2v" : "wan-i2v",
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
  references: endFrameByScene.has(scene.id)
    ? [scene.sourceImage, endFrameByScene.get(scene.id)]
    : [scene.sourceImage],
  output: scene.asset,
  // Generate at least the policy floor, then let the master timeline trim the
  // source clip to the exact scene duration. This gives very short beats
  // enough real temporal material for the motion-quality gate.
  duration: Math.max(scene.duration, qualityPolicy.minimumClipSeconds),
  width: 1280,
  height: 720,
  fps: 16,
  sourceCaptionIds: scene.sourceCaptionIds,
  highRisk: scene.highRisk,
  representativeSample: videoSampleIds.has(scene.id),
  maxAttempts: 3,
  timeoutSeconds: 7200,
}));

const jobs = [...anchors, ...coverDrafts, ...covers, ...keyframes, ...motionKeyframes, ...videos];
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
    "production/locations.json",
    "production/motion-endframes.json",
    "STORYBOARD_VIDEO.json",
  ],
  policy: "production/quality-policy.json",
  counts: {
    anchors: anchors.length,
    coverDrafts: coverDrafts.length,
    covers: covers.length,
    keyframes: keyframes.length,
    motionKeyframes: motionKeyframes.length,
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
