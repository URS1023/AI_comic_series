# AMD remote setup and troubleshooting

## Hardware truth

The physical host may contain eight Navi 31 cards, but a Jupyter container can expose only one. Trust these in order:

1. `/dev/kfd` and `/dev/dri/renderD*` inside the container;
2. `rocm-smi` visible devices;
3. verified ROCm PyTorch `torch.cuda.device_count()` and device properties.

The control plane reports all three. It never assumes host inventory equals allocation.

## Preferred direct control after one bootstrap

Use `scripts/direct-session.ps1` with a fresh read-only `api/contents` Copy-as-cURL. Jupyter is used only to upload `remote/direct_gateway.py` and `remote/start_direct_gateway.py` and start the loopback gateway. The local process generates a random bearer, injects it only into the gateway process environment, verifies the pinned Cloudflared binary, waits for the HTTPS origin, constructs `DirectManager`, and closes Jupyter before accepting production commands.

The direct gateway binds only `127.0.0.1`, never logs requests, rejects redirects and path traversal, exposes no arbitrary command execution, and removes the bearer and all Jupyter variables from Cloudflared and generation child environments. Code sync acknowledgements and downloaded artifacts are SHA-256 verified. A server reset or lost tunnel requires a new one-time bootstrap; already detached GPU jobs continue independently.

## Correcting the reset image

The initial reset image observed in this project had:

- a 20 GiB `/workspace` filesystem with about 11 GiB free;
- ComfyUI cloned but not running;
- no models or useful custom nodes;
- no FFmpeg;
- a CUDA build of PyTorch that saw zero AMD devices;
- ROCm 7.2.1 wheels present at `/`.

`remote/bootstrap.py` deliberately creates a new runtime on the large mount instead of mutating the small `/workspace/venv`. It installs the local ROCm wheels first and fails unless `torch.version.hip` is set and at least one GPU is available.

## Inspect jobs

```powershell
.\scripts\remote.ps1 remote status bootstrap
.\scripts\remote.ps1 remote logs bootstrap
.\scripts\remote.ps1 remote status models
.\scripts\remote.ps1 remote logs models
.\scripts\remote.ps1 remote status generate-videos
```

All status and log files are credential-free.

## Authentication failure

Copy a complete, read-only Jupyter Contents request with **Copy as cURL**. `scripts/remote.ps1` imports it from clipboard or stdin and replays that exact GET as a preflight before any remote action. The importer rejects non-HTTPS/non-AMD targets, a different-instance Referer, body-carrying requests, redirects, incomplete browser headers, and a mismatched XSRF header/cookie pair.

If AMD returns 401/403, or its precise masked `404 {"detail":"Instance not found"}` reply, the in-memory session refreshes once and safely replays the request. An ordinary missing-file 404 is never treated as an auth failure. If refresh still fails, log in again, copy a new complete cURL, and rerun the command. Never paste cookies into config, `.env`, argv, or a source file.

## Model validation error

If ComfyUI `/prompt` reports missing input names, first confirm the pinned ComfyUI commit and run `object_info` against the real node. Update the project API graph and its test; do not bypass node validation or switch to an unpinned custom node silently.

## Out of memory

Keep the final profile on Wan 2.2 A14B. Reduce generation resolution for a representative test, enable model offload, or run fewer concurrent workers before considering a lower-quality model. The TI2V 5B profile is an environment smoke test, not an automatic final fallback.
