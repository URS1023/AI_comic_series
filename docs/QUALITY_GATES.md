# Quality gates

Completion is proven only by evidence from the final encoded MP4 and its exact source graph.

## 1. Source and timing

- `SCRIPT_SOURCE.md` SHA matches the supplied source copy.
- Caption semantics audit: zero short split tails, zero overlong captions, terminal punctuation present.
- Voice alignment: every caption covered exactly once; chronological order; start drift `≤ 0.12s`; no scene crosses a chapter boundary.
- Density: no unexplained scene over 12 seconds; no scene over 16 seconds.

Current timing baseline: 42 captions, 29 scenes, `125.745s`, maximum drift `0.100s`, average scene `4.055s`, maximum scene `5.463s`.

## 2. Identity and keyframes

- All four identity anchors visibly inspected before scene generation.
- `production/anchor-approval.json` binds the four inspected anchor SHA-256 values; any later change invalidates the gate.
- Every keyframe compared against required anchors.
- Exact participant count and allowed roster.
- Correct age, face, hair, recognition marks, body and wardrobe.
- No generated text, watermark, red guide geometry or repeated/duplicate frame.
- High-risk hands opened at 100%; every visible fingertip traces to one palm, wrist and arm.
- `production/keyframe-approval.json` binds all 29 inspected keyframe SHA-256 values; one rejected frame blocks video generation.
- Phones, railings, shoes, chairs, doors and body support have plausible orientation and contact.

## 3. Real-video gate

- All 29 final storyboard assets exist and are MP4.
- No final shot is produced from a still with only crop/zoom/pan.
- Representative Wan sample pass rate is at least 90% before full batch.
- The fixed representative sample contains 10 shots spanning close face, two-person office action, phone handling, running, rain, railing, rescue contact and final reaction; `production/video-sample-approval.json` records accepted/rejected IDs and pass rate.
- Each clip compared with source keyframe for identity, outfit, first-frame composition, hands, camera stability and unintended object creation.
- Reject rubber motion, face morphs, edge warping, micro-jitter, frozen frames, new objects and changed final-frame composition.

## 4. Composition

- `node scripts/build-composition.mjs . --strict-assets` passes.
- `hyperframes lint` zero findings.
- seam ledger has one row per boundary and passes the motion-doctrine Seam Gate.
- `hyperframes check --strict --snapshots` passes runtime, layout, motion and contrast.
- HTML and ASS caption styles both derive from `HBG_STYLE.json`.
- Opening visibly contains the spoken lead, nine flash labels and complete selected-life title.

## 5. Audio

- Narration intelligible at natural speed.
- BGM source loudness measured (`-15.4 LUFS`, `-0.3 dBTP`) and reviewed at the configured `0.22` baseline.
- Rain begins with the rain scene and never masks narration.
- Final mix retains at least 3 dB true-peak headroom.
- No unexplained silence; audio and video duration agree within one frame.

## 6. Final encoded MP4

- H.264, `1920×1080`, 30 fps, yuv420p; AAC 48 kHz.
- Expected duration and frame count within one frame.
- Zero unexplained black-frame detections.
- Inspect opening, every corrected/high-risk shot, phone shot, railing/grab/fall sequence, body transitions and final frame.
- Build a contact sheet covering every actual rendered scene, not only an even sample.
- Record final SHA-256 and keep the previous verified revision.
