# 模板 06：写 Commit

> 用途：规范提交信息与提交拆分。本项目要求**小步本地提交、严禁 push**。

## 模板正文

```
【Context 背景】
项目：康伴 3D 数字人康养陪伴系统。
提交规范参考：git log 现有风格（如 "Day1: 康伴 3D 数字人项目骨架"）。
本次改动：{改动摘要 + 涉及文件}。

【Task 任务】
1. 判断当前改动是否需要拆分为多个 commit（按逻辑单元拆分：配置/迁移/模块/测试/文档）；
2. 为每个 commit 起信息：type(scope): 一句话说清"为什么"；
3. 列出每个 commit 包含的文件清单，检查是否混入不相关改动或敏感文件。

【Constraints 约束】
- 严禁 git push / force push / 改写已有历史；
- .env 及任何密钥文件不得入库（提交前 git status 复核）；
- 高风险文件（auth/、alembic/versions/、app/tools、Dockerfile）必须已在人工审查通过后才能提交；
- 一个 commit 只做一个逻辑单元，不混合"功能 + 格式化"。

【Expected Result 预期结果】
commit 拆分表：序号 | 提交信息 | 文件清单；随后逐个执行 git add（点名文件）+ git commit，
并输出 git log --oneline 与 git status 复核结果。
```

## 本项目示例

```
【Context 背景】
项目：康伴 3D 数字人康养陪伴系统，Day2 第 1-2 步已完成。
现有风格：a95fa5c "Day1: 康伴 3D 数字人项目骨架"。
本次改动：pyproject/.env.example/config/README 配置更新；docs/prompts/ 7 个新模板文件。

【Task 任务】
拆分为两个 commit：①Day2 依赖与配置（pyproject、.env.example、config.py、README）；
②提示词模板库 v1（docs/prompts/ 共 7 文件）。

【Constraints 约束】
- 严禁 push；.env 不入库；
- 提交前 git status 复核无意外未跟踪文件。

【Expected Result 预期结果】
两个 commit 完成后输出：git log --oneline -4、git status（干净）、
git show --stat 各 commit 的文件清单。
```
