#!/usr/bin/env node

import fs from "node:fs";
import path from "node:path";

const root = path.resolve(process.argv[2] ?? ".");
const strictAssets = process.argv.includes("--strict-assets");
const htmlCaptions = !process.argv.includes("--no-html-captions");
const storyboard = JSON.parse(fs.readFileSync(path.join(root, "STORYBOARD_VIDEO.json"), "utf8"));
const audio = JSON.parse(fs.readFileSync(path.join(root, "audio_meta.json"), "utf8"));
const style = JSON.parse(fs.readFileSync(path.join(root, "HBG_STYLE.json"), "utf8"));
const spec = JSON.parse(fs.readFileSync(path.join(root, "PROJECT_SPEC.json"), "utf8"));
const width = Number(style.canvas.width);
const height = Number(style.canvas.height);
const duration = Number(audio.totalDuration);
const caption = style.captions;
const openingPath = String(audio.opening.previewVideo).replaceAll("\\", "/");
const narrationPath = String(audio.body.path).replaceAll("\\", "/");
const bgmPath = String(spec.audio.bgmLooped).replaceAll("\\", "/");
const rainPath = String(spec.audio.rainAmbience).replaceAll("\\", "/");
const rewindWhooshPath = String(spec.audio.rewindWhoosh).replaceAll("\\", "/");

const escapeHtml = (value) =>
  String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
const number = (value) => Number(value).toFixed(3).replace(/\.000$/, "");

const requiredAssets = [
  openingPath,
  narrationPath,
  bgmPath,
  rainPath,
  rewindWhooshPath,
  ...storyboard.map((scene) => scene.asset),
];
const missing = requiredAssets.filter((relative) => !fs.existsSync(path.join(root, relative)));
if (strictAssets && missing.length > 0) {
  throw new Error(`Missing final composition assets:\n${missing.join("\n")}`);
}

const sceneHtml = [
  `<div id="el-opening" class="scene-wrapper" data-hf-id="opening-wrapper">`,
  `  <video id="video-opening" class="clip scene-video" src="${escapeHtml(openingPath)}" data-start="0" data-duration="${number(audio.openingDuration)}" data-track-index="0" muted playsinline preload="auto"></video>`,
  `</div>`,
  ...storyboard.map(
    (scene) =>
      `<div id="el-${scene.id}" class="scene-wrapper" data-hf-id="${scene.id}-wrapper">\n` +
      `  <video id="video-${scene.id}" class="clip scene-video" src="${escapeHtml(scene.asset)}" data-start="${number(scene.start)}" data-duration="${number(scene.duration)}" data-track-index="0" muted playsinline preload="auto"></video>\n` +
      `</div>`,
  ),
].join("\n");

const captionsHtml = htmlCaptions
  ? audio.captions
  .map((item, index) => {
    const start = Number(audio.opening.bodyStart) + Number(item.start);
    return (
      `<div id="${item.id}" class="clip caption-rail" data-start="${number(start)}" data-duration="${number(item.duration)}" data-track-index="${10 + index}">` +
      `<div id="${item.id}-inner" class="caption-inner">${escapeHtml(item.text)}</div></div>`
    );
  })
  .join("\n")
  : "";

const systemStart = Number(storyboard[4].start) + 0.2;
const systemEnd = Number(storyboard[5].end) - 0.15;
const firstChapterStart = Number(audio.opening.bodyStart) + 0.05;
const finalFadeStart = duration - 0.7;

const htmlWithStyles = `<!doctype html>
<html lang="zh-CN" data-resolution="landscape">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=${width}, height=${height}" />
    <title>${escapeHtml(spec.title)}</title>
    <script src="https://cdn.jsdelivr.net/npm/gsap@3.14.2/dist/gsap.min.js"></script>
    <style>
      @font-face {
        font-family: "Noto Sans SC";
        src: url("assets/fonts/NotoSansSC-Variable.ttf") format("truetype");
        font-style: normal;
        font-weight: 100 900;
        font-display: block;
      }
      * { margin: 0; padding: 0; box-sizing: border-box; }
      html, body { width: ${width}px; height: ${height}px; overflow: hidden; background: #0F1014; }
      body { font-family: "Noto Sans SC", sans-serif; color: #F5F2EF; }
      #root {
        position: relative;
        width: ${width}px;
        height: ${height}px;
        overflow: hidden;
        background: var(--canvas-deep, var(--canvas, #0F1014));
        --canvas: #1C1410;
        --canvas-deep: #0F1014;
        --accent: #D8000F;
      }
      #stage-ground { position: absolute; inset: 0; background-color: #0F1014; z-index: 0; }
      .scene-wrapper { position: absolute; inset: 0; overflow: hidden; z-index: 1; transform-origin: 50% 50%; }
      .scene-video { position: absolute; inset: 0; width: 100%; height: 100%; object-fit: cover; background-color: #0F1014; }
      .caption-rail {
        position: absolute;
        inset: 0;
        display: flex;
        align-items: flex-end;
        justify-content: center;
        padding-bottom: ${Number(caption.bottom)}px;
        pointer-events: none;
        z-index: 30;
      }
      .caption-inner {
        display: block;
        max-width: ${Number(caption.maxWidth)}px;
        padding: ${caption.htmlPadding};
        border-radius: ${Number(caption.borderRadius)}px;
        color: ${caption.textColor};
        background-color: ${caption.backgroundRgba};
        font-family: "${escapeHtml(caption.fontFamily)}", sans-serif;
        font-size: ${Number(caption.fontSize)}px;
        font-weight: ${Number(caption.fontWeight)};
        line-height: ${Number(caption.lineHeight)};
        letter-spacing: ${Number(caption.letterSpacingEm)}em;
        text-align: center;
        text-wrap: balance;
      }
      .system-score {
        position: absolute;
        inset: 0;
        display: flex;
        align-items: center;
        justify-content: flex-end;
        padding-right: 118px;
        z-index: 20;
      }
      .score-card, .system-panel {
        display: block;
        color: #F5F2EF;
        background-color: rgba(15, 16, 20, 0.86);
        border: 2px solid rgba(207, 234, 255, 0.62);
        border-radius: 0;
        box-shadow: none;
      }
      .score-card { width: 500px; padding: 40px 46px 44px; }
      .score-kicker, .system-label {
        font-family: "JetBrains Mono", monospace;
        font-size: 24px;
        font-weight: 700;
        letter-spacing: 0.12em;
        color: #C9C2BC;
      }
      .score-value {
        display: block;
        margin-top: 10px;
        font-family: "JetBrains Mono", monospace;
        font-size: 112px;
        font-weight: 700;
        line-height: 1;
        color: #D8000F;
        font-variant-numeric: tabular-nums;
      }
      .system-overlay {
        position: absolute;
        inset: 0;
        display: flex;
        align-items: center;
        justify-content: flex-end;
        padding-right: 96px;
        z-index: 20;
      }
      .system-panel { width: 610px; padding: 34px 40px; }
      .system-row {
        display: grid;
        grid-template-columns: 1fr auto;
        gap: 38px;
        align-items: baseline;
        min-height: 70px;
        padding: 15px 0;
        border-bottom: 2px solid rgba(207, 234, 255, 0.19);
      }
      .system-row:last-child { border-bottom: 0; }
      .system-value {
        font-family: "JetBrains Mono", "Noto Sans SC", monospace;
        font-size: 38px;
        font-weight: 700;
        color: #F5F2EF;
        font-variant-numeric: tabular-nums;
      }
      .system-row.emphasis .system-value { color: #D8000F; font-size: 50px; }
      .chapter-label {
        position: absolute;
        inset: 0;
        display: flex;
        align-items: flex-start;
        justify-content: flex-start;
        padding: 72px 84px;
        z-index: 18;
      }
      .chapter-card {
        display: block;
        max-width: 760px;
        padding: 18px 26px 22px;
        color: #FFF8EF;
        background-color: rgba(22, 16, 14, 0.82);
        border-left: 4px solid #D8000F;
      }
      .chapter-kicker { font-family: "JetBrains Mono", monospace; font-size: 24px; letter-spacing: 0.14em; }
      .chapter-title { margin-top: 5px; font-size: 48px; font-weight: 900; letter-spacing: -0.03em; }
      .final-shade { position: absolute; inset: 0; background-color: #0F1014; z-index: 50; }
      audio { display: none; }
    </style>
  </head>
  <body>
    <div id="root" data-composition-id="main" data-start="0" data-duration="${number(duration)}" data-width="${width}" data-height="${height}">
      <div id="stage-ground"></div>
      ${sceneHtml}

      <audio id="opening-audio" class="clip" src="${escapeHtml(openingPath)}" data-start="0" data-duration="${number(audio.openingDuration)}" data-track-index="2" data-volume="1"></audio>
      <audio id="body-narration" class="clip" src="${escapeHtml(narrationPath)}" data-start="${number(audio.opening.bodyStart)}" data-duration="${number(audio.narrationDuration)}" data-track-index="2" data-volume="1"></audio>
      <audio id="bgm" class="clip" src="${escapeHtml(bgmPath)}" data-start="0" data-duration="${number(duration)}" data-track-index="1" data-volume="${Number(style.audio.bgmVolume)}"></audio>
      <audio id="rain-ambience" class="clip" src="${escapeHtml(rainPath)}" data-start="${number(storyboard[16].start)}" data-duration="${number(duration - storyboard[16].start)}" data-track-index="3" data-volume="0.13"></audio>
      <audio id="rewind-whoosh" class="clip" src="${escapeHtml(rewindWhooshPath)}" data-start="${number(storyboard[1].start)}" data-duration="0.6" data-track-index="4" data-volume="0.62"></audio>

      <div id="score-overlay" class="clip system-score" data-start="${number(storyboard[0].start + 0.35)}" data-duration="3.25" data-track-index="6">
        <div id="score-card" class="score-card"><div class="score-kicker">GAOKAO SCORE</div><span class="score-value">426</span></div>
      </div>

      <div id="system-overlay" class="clip system-overlay" data-start="${number(systemStart)}" data-duration="${number(systemEnd - systemStart)}" data-track-index="6">
        <div id="system-panel" class="system-panel">
          <div class="system-row"><span class="system-label">NAME</span><span class="system-value">陈言</span></div>
          <div class="system-row emphasis"><span class="system-label">SCORE</span><span class="system-value">426</span></div>
          <div class="system-row"><span class="system-label">TO FULL SCORE</span><span class="system-value">324</span></div>
          <div class="system-row"><span class="system-label">REWIND</span><span class="system-value">高考前三天</span></div>
        </div>
      </div>

      <div id="chapter-label" class="clip chapter-label" data-start="${number(firstChapterStart)}" data-duration="3.1" data-track-index="7">
        <div id="chapter-card" class="chapter-card"><div class="chapter-kicker">ACT I</div><div class="chapter-title">回档前三天</div></div>
      </div>

      ${captionsHtml}
      <div id="final-fade" class="clip" data-start="${number(finalFadeStart)}" data-duration="0.7" data-track-index="40"><div id="final-shade" class="final-shade"></div></div>
    </div>

    <script>
      window.__timelines = window.__timelines || {};
      const tl = gsap.timeline({ paused: true });
      window.__timelines["main"] = tl;
      // <seams:auto>
      // Generated by scripts/stamp-seams.mjs.
      // </seams:auto>

      tl.fromTo("#score-card", { x: 90, autoAlpha: 0 }, { x: 0, autoAlpha: 1, duration: 0.38, ease: "power4.out", immediateRender: false }, ${number(storyboard[0].start + 0.35)});
      tl.fromTo("#system-panel", { x: 120, autoAlpha: 0 }, { x: 0, autoAlpha: 1, duration: 0.42, ease: "power4.out", immediateRender: false }, ${number(systemStart)});
      tl.fromTo("#system-panel .system-row", { y: 48, autoAlpha: 0 }, { y: 0, autoAlpha: 1, duration: 0.18, stagger: 0.075, ease: "power4.out", immediateRender: false }, ${number(systemStart + 0.16)});
      tl.fromTo("#chapter-card", { y: 62, autoAlpha: 0 }, { y: 0, autoAlpha: 1, duration: 0.28, ease: "power4.out", immediateRender: false }, ${number(firstChapterStart)});
      ${htmlCaptions
        ? audio.captions
        .map((item) => {
          const start = Number(audio.opening.bodyStart) + Number(item.start);
          return `tl.fromTo("#${item.id}-inner", { y: 24, autoAlpha: 0 }, { y: 0, autoAlpha: 1, duration: 0.16, ease: "power4.out", immediateRender: false }, ${number(start)});`;
        })
        .join("\n      ")
        : ""}
      tl.fromTo("#final-shade", { autoAlpha: 0 }, { autoAlpha: 1, duration: 0.7, ease: "power2.in", immediateRender: false }, ${number(finalFadeStart)});
    </script>
  </body>
</html>
`;

const styleMatch = htmlWithStyles.match(/    <style>\n([\s\S]*?)    <\/style>\n/);
if (!styleMatch) throw new Error("Generated composition is missing its style block");
const stylesDir = path.join(root, "styles");
fs.mkdirSync(stylesDir, { recursive: true });
fs.writeFileSync(path.join(stylesDir, "composition.css"), styleMatch[1]);
const html = htmlWithStyles.replace(
  styleMatch[0],
  '    <link rel="stylesheet" href="styles/composition.css" />\n',
);
fs.writeFileSync(path.join(root, "index.html"), html);
console.log(
  JSON.stringify(
    {
      status: "built",
      output: path.join(root, "index.html"),
      duration,
      scenes: storyboard.length,
      captions: audio.captions.length,
      htmlCaptions,
      missingAssets: missing,
      strictAssets,
    },
    null,
    2,
  ),
);
