[English](release-notes-v0.1.3.md) | [简体中文](release-notes-v0.1.3.zh-CN.md)

# sable v0.1.3

一次维护发版。2026 年 11 月 10 日。

引擎本身没有任何改动。WAL 还是那个自描述的 21 字节帧，SSTable 还是走
`temp -> fsync -> rename -> fsync(parent)` 发布，读路径还是在墓碑处*停下*，公开 API 一个没动。
0.1.2 写出来的库用 0.1.3 打开照常，不需要迁移。也没有运行时依赖要更：`sable` 依然只用标准库。

## 这一版里有什么

- **dev 工具链里的 `ruff` 从 0.17.3 升到 0.17.4。** 这是 linter 的一个补丁版本，主要是缺陷修复和
  格式化器的性能。lint job 是从 `.[dev]` 装的，所以这次改动上那套全绿的 CI，跑的就是 0.17.4，
  不是旧的固定值。
- **两份贡献指南打印的版本号，和实际装到的对上了。** `CONTRIBUTING.md` 与
  `CONTRIBUTING.zh-CN.md` 的环境自检里原本写着 `0.17.3`，现在改成 `0.17.4`。指南里的数字和
  `pyproject.toml` 里的又一致了。
- **CHANGELOG 的欠账清了。** 确定性的 PR 评审报告和迁到 H.I.F 组织这两条，从 v0.1.2 起一直挂在
  `[未发布]` 段，这次归进正式的版本历史。

## 这一版里没有什么

引擎没动，没有新增运行时依赖，格式没变，也没有重跑基准。`docs/` 里引用的每个测量值，都还是它
最初那份报告里的那个。linter 升级只影响贡献者和 CI；从这个 tag 装 `sable`，拿到的代码和 0.1.2
一样，只是版本字符串不同。

## 升级

```sh
pip install --upgrade "sable @ git+https://github.com/H-I-F/sable.git@v0.1.3"
python -c "import sable; print(sable.__version__)"   # 0.1.3
```

- 仓库：<https://github.com/H-I-F/sable>
- 发布页：<https://github.com/H-I-F/sable/releases/tag/v0.1.3>
- 完整变更：[CHANGELOG.zh-CN.md](../CHANGELOG.zh-CN.md) · [English](../CHANGELOG.md)
- 工程笔记：[索引](README.zh-CN.md) · [English](README.md)
