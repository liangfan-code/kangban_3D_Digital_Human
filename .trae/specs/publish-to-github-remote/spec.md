# 发布到 GitHub 远程仓库 Spec

## Why
当前工作目录 `kangban_3D_Digital_Human-main` 没有 `.git`，项目只存在于本地，无法做版本管理，也无法交付给协作者。需要把本地项目初始化为 Git 仓库并推送到用户自己的 GitHub 仓库 `liangfan-code/kangban_3D_Digital_Human`。

## What Changes
- 对当前目录执行 `git init`，默认分支设为 `main`
- 审查并确认 `.gitignore` 覆盖所有敏感与体积大的内容（`.env*`、`__pycache__`、`.venv`、`/models/`、`/funasr-runtime-resources/`）
- 校验 `.env.example` 中不含真实密钥（仅占位符），且工作区不存在 `.env` 等真实配置文件
- 暂存全部受版本管理的内容并创建首个提交
- 添加 remote `origin` = `git@github.com:liangfan-code/kangban_3D_Digital_Human.git`（SSH，已验证 `liangfan-code` 免密可用）
- 推送 `main` 并设置上游跟踪（`git push -u origin main`）
- 若网络直连失败，使用本地代理 `http://127.0.0.1:7897`（已有历史经验）

## Impact
- 受影响能力：项目版本管理、远程发布
- 受影响文件/系统：新增 `.git/`；`AGENTS.md`、`.gitignore`、`alembic.ini`、`pyproject.toml`、`uv.lock`、`app/`、`alembic/`、`docs/`、`scripts/`、`tests/`、`README.md`、`compose.funasr.yaml`、`.env.example` 首次入库
- 不改动任何业务代码、配置内容或数据库结构

## ADDED Requirements

### Requirement: 本地 Git 仓库初始化
系统 SHALL 在当前项目根目录创建 Git 仓库，默认分支为 `main`，且不覆盖、不删除任何已有文件。

#### Scenario: 首次初始化成功
- **WHEN** 在项目根目录执行 `git init` 并设置分支为 `main`
- **THEN** 生成 `.git/`，`git status` 可正常输出，`git rev-parse --abbrev-ref HEAD` 返回 `main`

### Requirement: 敏感信息不入库
系统 SHALL 保证任何真实密钥、`.env` 文件、模型权重与运行时资源不被提交。

#### Scenario: 提交内容审查
- **WHEN** 执行 `git add -A` 后检查暂存区
- **THEN** 暂存区不包含 `.env`、`/models/`、`/funasr-runtime-resources/`、`.venv/`、`__pycache__/`；`.env.example` 仅含 `CHANGE_ME` 或空占位值

### Requirement: 推送到指定 GitHub 仓库
系统 SHALL 把本地 `main` 分支推送到 `git@github.com:liangfan-code/kangban_3D_Digital_Human.git`。

#### Scenario: 首次推送成功
- **WHEN** 用户已在 GitHub 创建空仓库（不初始化 README/LICENSE/.gitignore），执行 `git push -u origin main`
- **THEN** 推送成功，`git branch -vv` 显示 `main` 跟踪 `origin/main`，`git ls-remote origin` 返回对应提交哈希

#### Scenario: 网络不可直连
- **WHEN** 直连 GitHub 超时或失败
- **THEN** 通过代理 `http://127.0.0.1:7897` 重试，或明确告知用户失败原因与手工命令，不静默失败

## MODIFIED Requirements
无

## REMOVED Requirements
无
