# Third-party components and media

This project stores source code and frozen configuration locally. Third-party models and large generated assets are downloaded to the configured AMD data volume and are not committed to Git.

## Models

- ComfyUI — GPL-3.0; exact Git commit is pinned in `config/models.json`.
- Wan 2.2 — Apache-2.0 model family; exact Comfy-Org files, revisions, sizes and SHA-256 values are pinned in `config/models.json`.
- Qwen-Image-2512 and Qwen-Image-Edit-2511 — Apache-2.0 model families; exact Comfy-Org files, revisions, sizes and SHA-256 values are pinned in `config/models.json`.
- Noto Sans SC — SIL Open Font License 1.1; license text is in `assets/fonts/OFL-NotoSansSC.txt`.

## Music and sound effects

- “Echoes of Time” by Kevin MacLeod — CC BY 4.0. Full attribution and hashes are in `assets/audio/bgm/LICENSE.md`.
- Bundled click and whoosh effects resolved by the installed `media-use` skill; provenance is recorded in `.media/manifest.jsonl`.

## Workflow references

Official ComfyUI UI workflow templates are frozen under `workflows/comfyui/vendor/`. Their source repository commit is recorded in the project API workflow documents. The executable API graphs under `workflows/comfyui/api/` are project-local, readable adaptations for headless execution.

