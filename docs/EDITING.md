# Editing guide

## Change narration without changing the story

Edit only `SCRIPT.md`, preserving chapter headings and terminal punctuation. Then regenerate narration and timing:

```powershell
$env:HBG_PYTHON=(Resolve-Path '.\.venv\Scripts\python.exe').Path
node C:\Users\你的用户名\.codex\skills\hbg-life-simulation\scripts\build_narration.mjs
Remove-Item Env:HBG_PYTHON
node C:\Users\你的用户名\.codex\skills\hbg-life-simulation\scripts\build_voice_driven_storyboard.mjs . --target-max 5.5 --output STORYBOARD_V2.json --base-output STORYBOARD_V2_BASE.json
npm run build
```

A narration change reopens every timing, seam and caption audit. Do not hand-edit generated start/end values.

## Change a character

Update both:

1. `CHARACTERS.md` — human identity contract;
2. `production/characters.json` — exact anchor-generation prompt.

Then rebuild the queue. A changed anchor fingerprint invalidates every scene that references it. Approve the new anchor before regenerating dependent keyframes.

## Change one scene

Edit the corresponding entry in `STORYBOARD_BASE.json` for the semantic beat, or `production/scene-overrides.json` when only the VTT-grouped final shot needs adjustment. Always set:

- exact `participants.count`;
- only allowed character IDs;
- one visible moment;
- a physical `motionTarget`;
- `highRisk: true` for hands, phones, overlaps, railing contact or falls.

Run `npm run build:storyboard`. Only the changed job and downstream video should receive a new fingerprint.

## Reject and regenerate a generated asset

Never overwrite a bad attempt manually. Move it under `assets/generated/rejected/`, record the reason in its job metadata or QA ledger, change the job seed/prompt, rebuild `generation-queue.json`, and rerun the relevant stage. Accepted siblings remain reusable.

For a Qwen keyframe with a locally removable defect, prefer the conservative two-pass route: add only that scene under `production/keyframe-revisions.json` with `mode: cleanup`, the current keyframe as `source`, a precise `defects` list and a seed offset. Rebuild the queue and rerun `keyframes`. The old keyframe becomes picture 1, approved character anchors become picture 2/3, and the prompt forbids changes outside the named defects. The remote worker archives the rejected prior output before landing the cleanup result.

## Change visual design

- Brand/frame tokens: `frame.md`.
- Caption pixels and safe area: `HBG_STYLE.json` only.
- System overlay layout: `scripts/build-composition.mjs`.
- Seam direction/technique: production docs builder and resulting `ledger.json`.

Do not duplicate caption constants in HTML or ASS; both builders read `HBG_STYLE.json`.

## Change models

Never replace only a filename. Add a new named profile to `config/models.json` with official repository, immutable commit, exact file size and SHA-256. Preserve the old profile so a verified render stays reproducible.
