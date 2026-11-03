[English](release-notes-v0.1.1.md) | [简体中文](release-notes-v0.1.1.zh-CN.md)

# sable v0.1.1

针对快照读的正确性修复：某个键被覆盖、而新旧版本仍同时存在于活跃 memtable 时，被钉住的视图
做的点查会出问题。

## 修复

- **快照点查现在解析到最新的*可见*版本。** 当一个键同时持有较新的、晚于快照的版本，以及视图
  仍有权读取的较旧版本时，`engine.snapshot().get(key)` 此前会返回「不存在」：memtable 查找只看
  最新版本，发现它不可见就放弃，哪怕同一张表里就躺着一个可见的旧版本。范围扫描本来就会按
  可见性过滤，于是它和点查对一个固定 sequence 该解析出什么给出了不同答案。`memtable.get_visible`
  现在沿版本链走，返回第一个不高于视图 sequence 的版本，与 SSTable reader 一直正确的行为对齐。
- **`Engine.open_snapshots`** 暴露未释放视图的数量，调用方无需伸手进引擎内部就能断言一次
  acquire/release 配平为零。

## 升级

`pip install --upgrade sable>=0.1.1`

---

这次修复背后的故事，上游视角写在
[当 `get` 和 `scan` 对一个快照给出不同答案](case-studies/snapshot-read-consistency.zh-CN.md)，
报告方视角写在
[sable-vector](https://github.com/H-I-F/sable-vector/blob/main/docs/case-studies/cross-layer-bugfix.zh-CN.md)。

- 仓库：<https://github.com/H-I-F/sable>
- 发布页：<https://github.com/H-I-F/sable/releases/tag/v0.1.1>
- 完整历史：[CHANGELOG.md](../CHANGELOG.md)
