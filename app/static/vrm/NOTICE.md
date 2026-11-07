# 3D 数字人资源来源与授权说明

本目录下的资源来自参考工程 `vrm-playground`（只读复制，未修改原仓库内容）。

## VRM 模型

- `models/Yoimiya.vrm`：复制自 `vrm-playground/public/assets/vrm/Yoimiya.vrm`（约 5.75 MB），默认形象。
- `models/Firefly.vrm`：复制自 `vrm-playground/public/assets/vrm/Firefly.vrm`（约 13.88 MB），用于验证形象切换；体积较大，平板加载慢于默认形象。
- `models/Neuvillette.vrm`：复制自 `vrm-playground/public/assets/vrm/Neuvillette.vrm`（约 6.28 MB），Day4 追加形象（康伴项目）。
- `models/Danheng.vrm`：复制自 `vrm-playground/public/assets/vrm/Danheng.vrm`（约 7.76 MB），Day4 追加形象（康伴项目）。
- `models/Amazonas.vrm`（约 2.7 MB）、`models/LilShark.vrm`（约 6.8 MB）、`models/AjoMajo.vrm`（约 2.1 MB）：复制自 `vrm-playground/public/assets/vrm/`，Day4 追加形象（用户自选，康伴项目）。
- `preview/`：模型挑选画廊（`avatar-gallery.html`）的候选资源快照，含 `preview/*.vrm`（vrm-playground 的 10 个候选模型）与 `preview/stage/*.glb`（vrm-playground 的 3 个舞台场景），版权同归原模型作者所有，仅限局域网内部演示。
- 参考工程来自 VRoid Hub Playground 风格演示，模型版权归原模型作者所有。
- **本项目仅将它们用于局域网内部演示**。正式交付前请确认模型作者授权。
- 形象选择由 `static/avatars.json` 决定：新增形象 = 把 `.vrm` 放进 `static/vrm/models/` + 在注册表加一条（无需改 JS）。
- 其他可选模型见 `vrm-playground/public/assets/vrm/`（Fischl、Linghua、Ningguang 等），本目录未全部复制以控制体积；扩充方式见上一条（零代码，只改注册表）。

## VRMA 动作

- `animations/common/*.vrma`：复制自 `vrm-playground/public/assets/vrm/animations/common/`。
- 上游来源：[Undi95/Hanami](https://github.com/Undi95/Hanami/tree/main/vrma)，许可为 Apache-2.0。
- 原文件名与内容保持不变；版权、转换说明与完整上游声明见 `animations/common/UPSTREAM-NOTICE.md`，许可正文见 `animations/common/LICENSE-Apache-2.0.txt`，逐文件 SHA-256 与时长见 `animations/common/manifest.json`。
- 本次只复制动作集实际引用到的 16 个文件（待机 2、说话 3、单次动作 11），未复制的动画见 `static/actions.json` 的 `inventory` / `unclassified`。

## 前端库

- `vendor/three/`：three.js 构建产物、`GLTFLoader` 与 `BufferGeometryUtils`，MIT 许可（见 `vendor/three/LICENSE`）。
- `vendor/three-vrm/`：`@pixiv/three-vrm` 构建产物，MIT 许可（见 `vendor/three-vrm/LICENSE`）。
- `vendor/three-vrm-animation/`：`@pixiv/three-vrm-animation` 构建产物，MIT 许可（见 `vendor/three-vrm-animation/LICENSE`）。

均为本地 vendored 文件，页面不依赖 CDN，可在无外网的内网环境运行。
