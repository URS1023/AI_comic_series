# ComfyUI API graph schema notes

The three executable API graphs are readable adaptations of official UI templates, pinned to:

- ComfyUI commit `7d39997e9f3897d8a50506bdc5f86dce844e0223`;
- workflow_templates commit `f79d2604ad3f93a22877b86deee45d8dfeb72245`.

Every remote submit calls `/object_info` and rejects unknown classes, unknown input names and missing required inputs before queueing GPU work. Local tests additionally reject dangling node references and missing media outputs.

Important non-obvious schemas verified against the pinned ComfyUI source:

- `FluxKontextMultiReferenceLatentMethod` input is `reference_latents_method`, with value `index_timestep_zero` for this project.
- `CFGNorm` requires `model` and `strength`; `pre_cfg` is optional and intentionally omitted (`false`).
- `TextEncodeQwenImageEditPlus` requires `clip` and `prompt`; `vae`, `image1`, `image2`, and `image3` are optional references.
- `WanImageToVideo` requires `positive`, `negative`, `vae`, `width`, `height`, `length`, and `batch_size`; `start_image` is supplied and `clip_vision_output` is intentionally omitted.
- `CreateVideo` requires `images` and `fps`; `audio` and `bit_depth` are optional.
- `SaveVideo.codec` is a DynamicCombo object, so the API value is `{ "codec": "auto" }`, not the string `"auto"`.

Do not copy `widgets_values` directly from a UI workflow into an API prompt: subgraphs and DynamicCombo widgets require expansion/typing that the browser normally performs.

