# 常用 VRMA 动作包

下载日期：2026-11-05。共 20 个动作，约 4.48 MB。

来源：[Undi95/Hanami](https://github.com/Undi95/Hanami/tree/main/vrma)。这些选中的动作全部由 Overte 动画转换而来，许可为 Apache-2.0。保留原文件名与内容；原始版权、转换说明和完整上游声明见 UPSTREAM-NOTICE.md，许可正文见 LICENSE-Apache-2.0.txt。

## 使用

在项目网页的动画面板点击选择文件，或将本目录中的 .vrma 文件拖入页面。建议先试 idle、idle-talking、nod、happy。坐姿动作需要自行配合椅子与角色位置；动作文件不包含家具。举手和点头等动作通常适合单次播放，待机和说话通常适合循环。

已使用本项目安装的 VRMAnimationLoaderPlugin 逐个解析，确认有有效动画和正数时长。尚未逐个在角色上目视检查；不同模型的比例可能影响手脚位置。

| 文件 | 中文说明 | 时长（秒） |
| --- | --- | --- |
| idle.vrma | 自然待机 | 10 |
| idle-2.vrma | 待机变化 | 30.033 |
| idle-talking.vrma | 站立说话 | 7.1 |
| idle-talking-6.vrma | 左手说话手势 | 16.633 |
| idle-talking-7.vrma | 右手说话手势 | 16.733 |
| nod.vrma | 点头 | 1.767 |
| nod-3.vrma | 点头同意 | 3.1 |
| shake.vrma | 摇头 | 3.633 |
| think.vrma | 环顾思考 | 3.367 |
| neutral.vrma | 歪头 | 5.067 |
| happy.vrma | 鼓掌 | 5.3 |
| happy-2.vrma | 鼓掌变化 | 3.767 |
| sad.vrma | 低头失落 | 3.267 |
| angry.vrma | 不满摇头 | 2.3 |
| relaxed.vrma | 伸展脖颈 | 5.567 |
| raise-hand.vrma | 举手 | 9.967 |
| world-walk-back.vrma | 后退行走 | 1.233 |
| world-sit-idle.vrma | 坐姿待机 | 13.333 |
| world-sit-talking.vrma | 坐姿说话 | 7.133 |
| world-sit-look.vrma | 坐姿环顾 | 10.767 |

manifest.json 记录每个文件的下载地址、SHA-256 和时长。
