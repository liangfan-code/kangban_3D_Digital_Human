# Tasks

- [x] Task 1: 审查工作区与提交内容安全性
  - [x] SubTask 1.1: 确认 `.gitignore` 覆盖 `.env*`、`/models/`、`/funasr-runtime-resources/`、`.venv/`、`__pycache__/`
  - [x] SubTask 1.2: 检查 `.env.example` 仅含占位符；确认工作区无 `.env` 真实配置
  - [x] SubTask 1.3: 统计待提交文件清单，确认无大体积二进制/模型权重

- [x] Task 2: 初始化本地 Git 仓库
  - [x] SubTask 2.1: `git init` 并将默认分支设为 `main`
  - [x] SubTask 2.2: 核对提交身份（梁帆 / 768020616@qq.com）

- [x] Task 3: 创建首个提交
  - [x] SubTask 3.1: `git add -A` 后复核暂存区不含敏感文件
  - [x] SubTask 3.2: 以 "初始提交：康伴 3D 数字人康养陪伴系统" 提交

- [x] Task 4: 配置 GitHub 远程并推送
  - [x] SubTask 4.1: 添加 remote `origin` = `git@github.com:liangfan-code/kangban_3D_Digital_Human.git`
  - [x] SubTask 4.2: 确认远端仓库存在（用户已在 GitHub 创建空仓库）
  - [x] SubTask 4.3: `git push -u origin main`；失败则走 `http://127.0.0.1:7897` 代理重试

- [x] Task 5: 推送后验证
  - [x] SubTask 5.1: `git branch -vv` 显示上游为 `origin/main`
  - [x] SubTask 5.2: `git ls-remote origin` 返回与本地 HEAD 一致的哈希

# Task Dependencies
- Task 2 依赖 Task 1
- Task 3 依赖 Task 2
- Task 4 依赖 Task 3，且依赖用户在 GitHub 网页完成空仓库创建
- Task 5 依赖 Task 4
