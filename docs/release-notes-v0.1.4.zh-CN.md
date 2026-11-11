[English](release-notes-v0.1.4.md) | [简体中文](release-notes-v0.1.4.zh-CN.md)

# sable v0.1.4

一次维护发版。2026 年 11 月 11 日。

引擎本身没有任何改动。WAL 还是那个自描述的 21 字节帧，SSTable 还是走
`temp -> fsync -> rename -> fsync(parent)` 发布，读路径还是在墓碑处*停下*，公开 API 一个没动。
0.1.3 写出来的库用 0.1.4 打开照常，不需要迁移。`sable` 依然只用标准库。

## 这一版里有什么

这一版记下的是仓库打理自己的方式变了。

- **依赖更新会自己合并。** 一个 `workflow_run` workflow 会在 Dependabot 的 PR 转绿之后给它挂上
  `auto-merge`。真正决定合不合的还是分支保护：必需检查、线性历史、所有对话已解决，都得过了才落地，
  所以 CI 挂了绝不会进 `main`。它在默认分支上跑，不需要额外 token 或第三方 action，也只碰
  Dependabot 开的 PR。
- **Dependabot 改成每月扫、分组提。** pip、GitHub Actions、Docker 三个生态从每周挪到每月，
  各自并成一个 PR。既然更新会自己合并，每周跑一次只是多产几个要关掉的 PR。
- **自动化安全修复打开了。** 常规更新慢下来，不代表报出来的漏洞也慢，它照样会有自己那条修复 PR。
- **贡献指南不再写死工具版本。** `CONTRIBUTING.md` 与 `CONTRIBUTING.zh-CN.md` 的环境自检原本
  打印固定的 `ruff`、`mypy` 数字，现在只说装到的版本应与 `.[dev]` 一致。bot 合入的 linter 升级
  不会再让指南留在旧号上。

## 以后什么才发版

不碰引擎的改动——依赖升级、CI 与 workflow 调整、bot 配置、文档——以后都走自动合并，不再逐个人工
复核，也不再单独发版。只有核心逻辑的调整，或者其他大到值得让装包的人专门读一眼的变更，才打 tag。

这一版就是把这条约定写下来的那一版，也是最后一次为纯维护改动发版。从它往后，版本号只在引擎动的时候
才动。

## 这一版里没有什么

引擎没动，没有新增运行时依赖，格式没变，也没有重跑基准。`docs/` 里引用的每个测量值，都还是它
最初那份报告里的那个。从这个 tag 装 `sable`，拿到的代码和 0.1.3 一样，只是版本字符串不同。

## 升级

```sh
pip install --upgrade "sable @ git+https://github.com/H-I-F/sable.git@v0.1.4"
python -c "import sable; print(sable.__version__)"   # 0.1.4
```

- 仓库：<https://github.com/H-I-F/sable>
- 发布页：<https://github.com/H-I-F/sable/releases/tag/v0.1.4>
- 完整变更：[CHANGELOG.zh-CN.md](../CHANGELOG.zh-CN.md) · [English](../CHANGELOG.md)
- 工程笔记：[索引](README.zh-CN.md) · [English](README.md)
