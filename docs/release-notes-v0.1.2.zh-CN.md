[English](release-notes-v0.1.2.md) | [简体中文](release-notes-v0.1.2.zh-CN.md)

# sable v0.1.2

一次文档发版。2026 年 11 月 3 日。

引擎本身没有任何改动。WAL 还是那个自描述的 21 字节帧，SSTable 还是走
`temp -> fsync -> rename -> fsync(parent)` 发布，读路径还是在墓碑处*停下*，公开 API 一个没动。
0.1.1 写出来的库用 0.1.2 打开照常，不需要迁移。版本号往前挪，是因为 0.1.1 之后做的那一批
文档值得有一个自己的 tag，不是因为有什么新行为要装。

## 这一版里有什么

`docs/` 下的工程笔记现在是一套完整的中英双语材料：

- **两份索引。** `docs/README.md` 与 `docs/README.zh-CN.md` 把仓库里每一篇都标了出来，附一张
  把概念指回模块的源码地图，以及一份从根 README 读到实测报告的阅读顺序。
- **既有长文的中文版。** 五篇 ADR、TTL 淘汰配方、实测报告、三篇 `docs/skills/` 指南，
  现在都各有 `.md` 与 `.zh-CN.md`；`CHANGELOG`、`CONTRIBUTING`、`SECURITY` 也是。
- **发版公告与案例复盘。** `docs/release-notes-v0.1.0.md`、`docs/release-notes-v0.1.1.md`、
  `docs/case-studies/snapshot-read-consistency.md`，均为双语。案例复盘从本站这一侧讲 v0.1.1
  的快照读修复，并链到 sable-vector 那边的下游记录。
- **演进台账拆干净了。** `PROGRESS.md` 原本是英文标题裹着中文条目，现在改成纯英文，中文交给
  `PROGRESS.zh-CN.md`；两份都保留同样的 32 条 PR 条目和 11 条链接定义。
- **README 接好了线。** 文档表格、每条都成对给出 English 与简体中文的治理清单，以及指向下游
  案例复盘的链接。

顺手修掉一处：`docs/release-notes-v0.1.1.md` 的标题打了两遍，删掉重复的那行。

## 这一版里没有什么

没有重跑基准，也没有重算任何数字。`docs/` 里引用的每个测量值，都还是它最初那份报告里的那个。
CHANGELOG 的 `[未发布]` 段仍然挂着确定性的 PR 评审报告和迁到 H.I.F 组织这两条——那是工具与治理
层面按自己节奏走的事，这个以文档为主题的 tag 不收它们。

## 升级

```sh
pip install --upgrade "sable @ git+https://github.com/H-I-F/sable.git@v0.1.2"
python -c "import sable; print(sable.__version__)"   # 0.1.2
```

- 仓库：<https://github.com/H-I-F/sable>
- 发布页：<https://github.com/H-I-F/sable/releases/tag/v0.1.2>
- 完整变更：[CHANGELOG.zh-CN.md](../CHANGELOG.zh-CN.md) · [English](../CHANGELOG.md)
- 工程笔记：[索引](README.zh-CN.md) · [English](README.md)
