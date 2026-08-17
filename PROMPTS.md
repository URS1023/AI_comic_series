# 生成提示词与调用账本

本文件只记录无密钥的最终提示词、调用 ID、模型版本、种子和验收结果。任何 API 密钥、Cookie、SSO/JWT、refresh token 或带签名下载 URL 都禁止写入。

## 视觉总约束

```text
Use case: illustration-story
Asset type: 16:9 Chinese webtoon identity anchor or episode keyframe
Style/medium: polished Chinese webtoon, grounded seinen manga, bold clean black ink contour, realistic anatomy, detailed lived-in environment, subtle halftone paper texture
Color palette: deep brown-black #1C1410, warm off-white #F5F2EF, restrained tomato-red #D8000F accents, story-appropriate natural colors
Constraints: exact participant count; only named characters; immutable identity and wardrobe from CHARACTERS.md; plausible anatomy and prop orientation; no sexualization of students
Avoid: any letters, Chinese characters, numbers, signs, logos, watermark, captions, speech bubbles, extra people, extra limbs, fused fingers, reversed phones, decorative red circles or guide marks
```

## 锚点调用

| ID | 角色 | 工具/模型 | 调用标识 | 最终文件 | 状态 |
| --- | --- | --- | --- | --- | --- |
| anchor-chen-yan | 陈言 | built-in ImageGen | pending | `assets/generated/anchors/chen-yan.png` | pending |
| anchor-teacher-wang | 王老师 | built-in ImageGen | pending | `assets/generated/anchors/teacher-wang.png` | pending |
| anchor-landlady | 房东小姐 | built-in ImageGen | pending | `assets/generated/anchors/landlady.png` | pending |
| anchor-riverside-girl | 江边女孩 | built-in ImageGen | pending | `assets/generated/anchors/riverside-girl.png` | pending |

## 剧情关键帧与视频调用

逐镜最终提示词由 `STORYBOARD_BASE.json` 的 `description`、`participants`、`sourceImage` 和 `motionTarget` 与对应角色锚点合成。每次生成完成后写入 `production/scene-manifest.json`；失败或拒绝的调用保留原因但不进入最终时间线。

