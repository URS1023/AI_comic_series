#!/usr/bin/env node

import fs from "node:fs";
import path from "node:path";

const projectRoot = path.resolve(process.argv[2] ?? ".");
const overridePath = path.join(projectRoot, "production", "scene-overrides.json");
const overridesDocument = JSON.parse(fs.readFileSync(overridePath, "utf8"));
const timedPath = path.join(projectRoot, overridesDocument.storyboardInput ?? "STORYBOARD_V2.json");
const timed = JSON.parse(fs.readFileSync(timedPath, "utf8"));
const semantic = JSON.parse(fs.readFileSync(path.join(projectRoot, "STORYBOARD_BASE.json"), "utf8"));
const semanticById = new Map(semantic.map((scene) => [scene.id, scene]));
const overrides = overridesDocument.scenes ?? {};

const errors = [];
const finalScenes = timed.map((scene) => {
  const override = overrides[scene.id];
  if (!override) {
    errors.push(`${scene.id}: missing semantic override`);
    return scene;
  }
  const source = semanticById.get(override.sourceSceneId);
  if (!source) {
    errors.push(`${scene.id}: unknown sourceSceneId ${override.sourceSceneId}`);
    return scene;
  }
  const participants = override.participants ?? source.participants;
  if (!participants || !Number.isInteger(Number(participants.count)) || !Array.isArray(participants.allowed)) {
    errors.push(`${scene.id}: invalid participants`);
  }
  return {
    ...scene,
    sourceSceneId: source.id,
    description: override.description ?? source.description,
    participants,
    shot: override.shot ?? source.shot,
    highRisk: override.highRisk ?? source.highRisk,
    motion: "source-video",
    dynamicEffect: null,
    sourceImage: `assets/generated/keyframes/${scene.id}.png`,
    asset: `assets/generated/video/${scene.id}.mp4`,
    motionTarget: override.motionTarget ?? source.motionTarget,
  };
});

for (const id of Object.keys(overrides)) {
  if (!timed.some((scene) => scene.id === id)) errors.push(`${id}: override has no timed scene`);
}
const assetSet = new Set(finalScenes.map((scene) => scene.asset));
if (assetSet.size !== finalScenes.length) errors.push("final assets are not unique");
if (errors.length > 0) throw new Error(`Video storyboard validation failed:\n${errors.join("\n")}`);

const outputPath = path.join(projectRoot, "STORYBOARD_VIDEO.json");
fs.writeFileSync(outputPath, `${JSON.stringify(finalScenes, null, 2)}\n`);
console.log(
  JSON.stringify(
    {
      status: "built",
      output: outputPath,
      scenes: finalScenes.length,
      highRisk: finalScenes.filter((scene) => scene.highRisk).length,
      videoAssets: assetSet.size,
      captions: finalScenes.reduce((total, scene) => total + scene.sourceCaptionIds.length, 0),
    },
    null,
    2,
  ),
);

