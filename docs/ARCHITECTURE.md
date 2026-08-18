# Architecture

## Design goals

1. **Local is authoritative.** Remote state may be reset without losing source, prompts, queue state or final media.
2. **Remote is replaceable.** The AMD node contains no only copy of code or accepted output.
3. **Secrets are process-local.** Complete browser Copy-as-cURL requests enter through stdin, are redacted from representations, and are never serialized.
4. **Every expensive result is fingerprinted.** Re-running unchanged work reuses outputs; changed inputs invalidate only affected jobs.
5. **Final story pixels come from video.** Still motion is not a production fallback.

## Local layers

- `BRIEF.md` — why the video exists and the non-negotiable user constraints.
- `SCRIPT_SOURCE.md` — verbatim 39-chapter source kept locally; `SOURCE_MANIFEST.json` is the Git-tracked identity record.
- `SCRIPT.md` — episode narration adaptation.
- `CHARACTERS.md` — human-readable immutable identity and environment rules.
- `production/characters.json` — machine-readable anchor prompts.
- `STORYBOARD_BASE.json` — 25 semantic story actions.
- `STORYBOARD_V2.json` — VTT-driven caption grouping.
- `STORYBOARD_VIDEO.json` — final 29 all-video timeline.
- `production/generation-queue.json` — 81 resumable jobs: 4 identity anchors, 7 location anchors, 2 cover drafts, 2 identity-corrected cover artworks, 29 start keyframes, 8 reviewed motion endframes and 29 videos.
- `assets/generated/prompts/SHEET_MAP.json` — 29 个独立关键帧任务与最终场景的一对一覆盖证明。
- `production/*-approval.json` — 绑定实际 SHA-256 的锚点、关键帧、动作末帧、代表样片与 29/29 正式视频审核门。
- `frame.md` / `HBG_STYLE.json` — visual and caption truth.
- `ledger.json` — 29 seam vectors.
- `index.html` — generated HyperFrames master composition.

## Control plane

`src/ai_comic_series/` deliberately stays shallow:

- `config.py` validates non-secret config and process-local credentials.
- `curl_import.py` strictly parses Copy-as-cURL, derives the active instance, and preserves the required browser header context without exposing values.
- `auth.py` performs in-memory AMD SSO refresh and Cookie rotation.
- `jupyter.py` owns authenticated REST, WebSocket execution and small-file transfer.
- `remote_manager.py` synchronizes the bundle, starts detached jobs, reads status, fetches and verifies artifacts.
- `direct_transport.py` supplies the bearer-only HTTPS transport after one-time bootstrap; redirects, path traversal and digest mismatches are rejected.
- `direct_session.py` uploads only the loopback gateway through Jupyter, starts the verified tunnel, closes Jupyter and keeps the direct bearer only in memory.
- `cli.py` is the local `comicctl` boundary.

No module reads browser profiles, credential stores or unrelated files.

## Remote data volume

The selected large mount contains:

```text
ai-comic-series/
├── runtime/ComfyUI/       # pinned commit
├── runtime/venv/          # ROCm PyTorch environment
├── models/                # SHA-verified model files
├── cache/huggingface/     # resumable downloads
├── comfy-input/
├── comfy-output/gpuN/
├── project-assets/        # accepted anchors/keyframes/videos
├── logs/
├── processes/
└── work/
```

The small Jupyter-visible project directory stores only `remote/`, `config/`, `workflows/`, `production/`, public status JSON, logs and short-lived artifact mirrors. Mirrors are deleted after local SHA verification unless explicitly retained.

## GPU scheduling

The bootstrap trusts `torch.cuda.device_count()` from a verified ROCm build, not the host's physical inventory. It executes FP16/BF16 compute on every visible device, then starts one project-marked ComfyUI process per GPU on dedicated ports `18888 + index`. A resource queue guarantees at most one active prompt per port; a one-GPU container remains fully functional.

## Failure model

- Model download: Hugging Face cache resumes; target is materialized only after size and SHA match.
- Generation: up to three bounded attempts; successful jobs land immediately and are never rolled back with failed siblings.
- Review gates: keyframes require hash-bound identity/location-anchor approval; FLF2V requires reviewed start and end frames; every high-risk representative must pass; final render requires hash-bound visual approval of all 29 videos.
- Local disconnect: remote jobs are detached and continue; status is read later.
- Token expiration: Jupyter 401/403 or the exact AMD masked instance 404 triggers in-memory AMD refresh and one safe replay.
- Stop: `production/STOP` prevents the next queued job while preserving all accepted outputs.
- Remote reset: rerun bootstrap and model install, then resume from local queue and accepted local artifacts.
