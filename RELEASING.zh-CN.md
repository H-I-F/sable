[English](./RELEASING.md) | 简体中文

# 发版约定

这份是「什么改动才发版」的现行规则，是一份活文档。如果它和
`.github/scripts/change_policy.py` 说法不一致，以脚本为准——CI 和自动合并
跑的是脚本，不是这一页。

## 只有一条规则

只有引擎变了，版本号才动。依赖升级、CI 与 workflow 调整、bot 配置、文档，
这些改动照常合并，不单独打 tag。

某次改动属于哪一档，不是 review 时临时拍脑袋定的，而是由
`.github/scripts/change_policy.py` 根据改动文件算出来的。所有使用方读的是
同一个判定：

| 档位 | 什么会触发 | 人工复核 | 自动合并 | 发版 |
| --- | --- | --- | --- | --- |
| `core` | `src/` 下的任何文件，或 `pyproject.toml` 里的运行时依赖 | 需要 | 否 | 是 |
| `review` | 基础镜像或 action 引用变了（`FROM python:3.11` → `3.15`、`@v4` → `@v7`） | 需要 | 否 | 否，除非同时改了 `src/` |
| `routine` | 文档、CI、workflow、bot 配置、dev 工具 pin、版本号元数据 | 不需要 | 是 | 否 |

改动文件列表为空或格式坏掉，一律算错误，绝不当成 `routine`。
`change_policy.py --self-test` 和 `release_tool.py --self-test` 都会先证明
各自的检测器还能触发。`policy-check.yml` 在每次 push 和 PR 上跑这两个自检，
再跑一次 `release_tool.py check`，而它是必需的状态检查。

## 发一次版

在干净的 `main` 检出上执行：

```sh
REPO=$(gh repo view --json nameWithOwner --jq .nameWithOwner)
BASE=$(gh api "repos/$REPO/releases/latest" --jq .tag_name)

# 1. 这次真的该发版吗？从标准输入读 compare 结果。
gh api "repos/$REPO/compare/$BASE...main" | python3 .github/scripts/release_tool.py eligible

# 2. 只做机械改动：版本源、双语 CHANGELOG、compare/tag 链接。
python3 .github/scripts/release_tool.py prepare 0.1.5 2026-12-01

# 3. 手写两份公告、更新四处索引；只要有一处对不上，check 就会拒绝。
python3 .github/scripts/release_tool.py check
```

然后用英文提交信息开 PR，等 CI 跑完、squash 合并，再建 tag 和 Release。
正文刻意留给人写：公告和索引是给人读的，不该由工具填空。

`prepare` 只做那些能带命中断言的部分，不会替你编公告；`check` 会拒绝
版本源、双语 CHANGELOG、compare/tag 链接、公告、索引没有指向同一个版本号的树。

## 日常改动

凡是依赖机器人提的、且不属于 `core` 的改动，都不需要维护者点合并：自动合并
workflow 会向 `change_policy.py` 要一个判定，只对 `routine` 打开自动合并。
分支保护仍然有最终决定权，`review` 档会留着等人看一眼。

reviewer 一侧的规矩见 [CONTRIBUTING.md](CONTRIBUTING.md)，实际交付了什么见
[CHANGELOG.md](CHANGELOG.md)。
