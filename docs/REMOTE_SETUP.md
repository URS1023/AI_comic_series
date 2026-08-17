# AMD remote setup and troubleshooting

## Hardware truth

The physical host may contain eight Navi 31 cards, but a Jupyter container can expose only one. Trust these in order:

1. `/dev/kfd` and `/dev/dri/renderD*` inside the container;
2. `rocm-smi` visible devices;
3. verified ROCm PyTorch `torch.cuda.device_count()` and device properties.

The control plane reports all three. It never assumes host inventory equals allocation.

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

If AMD returns 401/403 and refresh also says invalid, the browser login itself has expired. Log in again and rerun the command; the hidden prompt accepts the new complete Cookie request-header value. Do not paste cookies into config or `.env`.

## Model validation error

If ComfyUI `/prompt` reports missing input names, first confirm the pinned ComfyUI commit and run `object_info` against the real node. Update the project API graph and its test; do not bypass node validation or switch to an unpinned custom node silently.

## Out of memory

Keep the final profile on Wan 2.2 A14B. Reduce generation resolution for a representative test, enable model offload, or run fewer concurrent workers before considering a lower-quality model. The TI2V 5B profile is an environment smoke test, not an automatic final fallback.

