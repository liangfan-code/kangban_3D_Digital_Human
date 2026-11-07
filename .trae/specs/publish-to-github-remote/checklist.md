# Checklist

- [ ] 仓库已初始化为 Git 仓库，当前分支为 `main`
- [ ] 暂存区与首个提交中不含 `.env`、`/models/`、`/funasr-runtime-resources/`、`.venv/`、`__pycache__/`
- [ ] `.env.example` 仅包含 `CHANGE_ME` 或空占位，无真实密钥
- [ ] 已添加 remote `origin` 指向 `git@github.com:liangfan-code/kangban_3D_Digital_Human.git`
- [ ] `main` 分支已成功推送到 GitHub，`git branch -vv` 显示跟踪 `origin/main`
- [ ] `git ls-remote origin` 返回的提交哈希与本地 HEAD 一致
- [ ] 全程未改动任何业务代码、迁移脚本或配置文件内容
