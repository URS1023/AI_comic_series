#!/usr/bin/env node

import fs from "node:fs";
import path from "node:path";

const root = path.resolve(process.argv[2] ?? ".");
const storyboard = JSON.parse(fs.readFileSync(path.join(root, "STORYBOARD_VIDEO.json"), "utf8"));
const audio = JSON.parse(fs.readFileSync(path.join(root, "audio_meta.json"), "utf8"));

const phaseFor = (index) => {
  if (index <= 1) return ["回档冲击", "现实被时间机制撕开，观众先感到失重和不可信。"];
  if (index <= 8) return ["办公室黑色幽默", "冷日光灯下的现实喜剧，反应真实、节奏干脆。"];
  if (index <= 11) return ["路线选择", "城市夜色打开一条未知支线，轻松感仍在但风险开始累积。"];
  if (index <= 15) return ["房东窥视", "温柔表面下的控制感，镜头安静、视线具有压迫。"];
  if (index <= 19) return ["暴雨预警", "空间变空、雨声变密，人物被江面和护栏压缩。"];
  if (index <= 23) return ["救援峰值", "动作明确、剪辑加速，物理关系比美术姿态更重要。"];
  return ["反常劝阻与悬念", "节奏重新放慢，陈言的反常台词迫使女孩和观众一起回头。"];
};

const transitionFor = (index) => {
  if (index === storyboard.length - 1) return "final hold → color dip to #0F1014, 0.7s";
  if (index === 0 || index === 15) return "forward zoom-through, Z+, full-frame blur 18px, 0.21s exit / 0.50s entry";
  if (index === storyboard.length - 2) return "inverse zoom-through ARRIVAL, Z−, full-frame blur 18px, 0.21s exit / 0.50s entry";
  return "cut-the-curve LEFT, 12% partial travel, 0.34s power4.in / 0.42s power4.out";
};

let expanded = `# Expanded production prompt｜高考回档：雨夜白月光\n\n`;
expanded += `## Title + style\n\n`;
expanded += `- Canvas: #1C1410; opaque seam ground: #0F1014; ink: #F5F2EF; system accent: #D8000F; rain blue: #20384A; lightning: #CFEAFF; sodium light: #D89A4A.\n`;
expanded += `- Type: Noto Sans SC 900 display / 600 captions from assets/fonts/NotoSansSC-Variable.ttf; JetBrains Mono 700 for system numbers.\n`;
expanded += `- Medium: polished Chinese webtoon, grounded seinen manga, realistic anatomy, full-bleed generated video.\n\n`;
expanded += `## Rhythm\n\n`;
expanded += `rewind-PUNCH → dry-comedy-SNAP → choice-TRAVEL → surveillance-HOLD → rain-BUILD → rescue-PEAK → cliffhanger-STILL. Total ${audio.totalDuration.toFixed(3)}s.\n\n`;
expanded += `## Global rules\n\n`;
expanded += `- The generated source video performs every story action. No final shot may be a still image with synthetic pan/zoom.\n`;
expanded += `- Film current is LEFT. Z-forward is reserved for rewind and entering the storm; Z-backward is reserved for the final reaction.\n`;
expanded += `- Captions are bottom overlays on the full frame, never a reserved band. System data is drawn locally on blank generated surfaces.\n`;
expanded += `- Video itself supplies rain, light, fabric and body motion; no idle wobble, random grain or decorative breathing.\n`;
expanded += `- Every scene carries its exact participants list from STORYBOARD_VIDEO.json; no other person may appear.\n\n`;

for (const [index, scene] of storyboard.entries()) {
  const [phase, mood] = phaseFor(index);
  expanded += `## Scene ${String(index + 1).padStart(2, "0")}｜${scene.id}\n\n`;
  expanded += `- **Timing:** ${scene.start.toFixed(3)}–${scene.end.toFixed(3)}s (${scene.duration.toFixed(3)}s); captions: ${scene.sourceCaptionIds.join(", ")}.\n`;
  expanded += `- **Narrative role:** ${phase}; spoken beat: “${scene.cue}”.\n`;
  expanded += `- **Concept:** ${scene.description} The viewer must read this as one concrete cinematic action, not an illustrated summary.\n`;
  expanded += `- **Mood:** ${mood}\n`;
  expanded += `- **Depth:** BG = story location/weather from CHARACTERS.md; MG = exact permitted characters (${scene.participants.allowed.join(", ")}); FG = rain/light or blank local-overlay surface plus caption rail.\n`;
  expanded += `- **Choreography:** ${scene.motionTarget}\n`;
  expanded += `- **Shot / risk:** ${scene.shot}; participants=${scene.participants.count}; highRisk=${scene.highRisk}.\n`;
  expanded += `- **Transition out:** ${transitionFor(index)}.\n`;
  expanded += `- **Assets:** keyframe ${scene.sourceImage}; required final video ${scene.asset}.\n\n`;
}

expanded += `## Recurring motifs\n\n`;
expanded += `- A thin tomato-red system line appears only with 426 / 324 / rewind data and disappears before the street sequence.\n`;
expanded += `- The green traffic light is the choice motif; sodium-orange light marks ordinary reality; blue-white lightning marks irreversible change.\n`;
expanded += `- Leftward screen travel carries ordinary story progress; the sudden right turn is caused by the horn and is visible inside the source clip.\n\n`;
expanded += `## Negative prompt\n\n`;
expanded += `No static-image disguised as video; no identity drift; no age or clothing changes; no extra people; no extra, fused or detached limbs; no unreadable/generated text; no watermark; no reversed phone; no impossible railing, gravity or body support; no sexualized student; no blood or graphic self-harm; no decorative red targets, circles or guides; no camera jitter; no rubber motion; no new objects between first and last frame.\n`;

const hyperframesDir = path.join(root, ".hyperframes");
fs.mkdirSync(hyperframesDir, { recursive: true });
fs.writeFileSync(path.join(hyperframesDir, "expanded-prompt.md"), expanded);

let board = `---\nmode: autonomous\nmessage: "高考失利的陈言回档三天，一次临时改道让他救下白月光，也让所有人的命运开始偏移。"\naudience: "国漫、时间循环与都市校园网文观众"\naspect: 1920x1080\nduration: ${audio.totalDuration.toFixed(3)}\n---\n\n`;
for (const [index, scene] of storyboard.entries()) {
  const [phase] = phaseFor(index);
  board += `## Frame ${scene.id}\n\n`;
  board += `status: outline\n`;
  board += `src: ${scene.asset}\n`;
  board += `timing: ${scene.start.toFixed(3)}-${scene.end.toFixed(3)}\n`;
  board += `motion: Wan2.2 source-video performance; seam=${transitionFor(index)}\n`;
  board += `beat: ${scene.cue}\n`;
  board += `narrativeRole: ${phase}\n`;
  board += `visual: ${scene.description}\n`;
  board += `participants: ${scene.participants.count} [${scene.participants.allowed.join(", ")}]\n`;
  board += `sourceImage: ${scene.sourceImage}\n\n`;
}
fs.writeFileSync(path.join(root, "STORYBOARD.md"), board);

const seams = [
  {
    id: `opening→${storyboard[0].id}`,
    cut: Number(storyboard[0].start.toFixed(3)),
    technique: "zoom-through forward",
    exit: { selector: "#el-opening", axis: "z", dir: 1 },
    entry: { selector: `#el-${storyboard[0].id}`, axis: "z", dir: 1, scanRoot: `#el-${storyboard[0].id}` },
    blur: 18,
  },
];
for (let index = 0; index < storyboard.length - 1; index += 1) {
  const from = storyboard[index];
  const to = storyboard[index + 1];
  const zoomForward = index === 0 || index === 15;
  const inverseArrival = index === storyboard.length - 2;
  const axis = zoomForward || inverseArrival ? "z" : "x";
  const direction = inverseArrival ? -1 : zoomForward ? 1 : -1;
  seams.push({
    id: `${from.id}→${to.id}`,
    cut: Number(to.start.toFixed(3)),
    technique: inverseArrival
      ? "inverse zoom-through"
      : zoomForward
        ? "zoom-through forward"
        : "cut-the-curve LEFT",
    exit: { selector: `#el-${from.id}`, axis, dir: direction },
    entry: {
      selector: `#el-${to.id}`,
      axis,
      dir: direction,
      ...(axis === "z" ? { scanRoot: `#el-${to.id}` } : {}),
    },
    ...(axis === "z" ? { blur: 18 } : {}),
  });
}
fs.writeFileSync(path.join(root, "ledger.json"), `${JSON.stringify({ fps: 30, seams }, null, 2)}\n`);

console.log(
  JSON.stringify(
    {
      expandedPrompt: path.join(hyperframesDir, "expanded-prompt.md"),
      storyboard: path.join(root, "STORYBOARD.md"),
      ledger: path.join(root, "ledger.json"),
      scenes: storyboard.length,
      seams: seams.length,
      duration: audio.totalDuration,
    },
    null,
    2,
  ),
);

