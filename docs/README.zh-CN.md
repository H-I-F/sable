[English](README.md) | [简体中文](README.zh-CN.md)

# sable — 工程知识库索引中枢

> 本目录是[项目主文档](../README.md)的长文配套。主文档面向*使用者*，这里的每一页都写给
> 想搞清楚「**为什么**每个字节都这样排布」、「并发模型为何在真实线程下依然正确」、
> 「哪些取舍是刻意为之」的人。
>
> 文中每一条结论都锚定在 `src/sable/` 里的真实符号。凡是提到「见 `engine.py`」，链接
> 指向的是源码文件本身，而非二手转述。

## 这里都有什么

| 板块 | 面向 | 形式 |
| --- | --- | --- |
| [工程实践 Skills](#工程实践-skills底层实现指南) | 实现或移植该引擎的工程师 | 带代码与表格的叙事指南 |
| [硬核问答 FAQ](#硬核问答-faq存储系统深度追问) | 面试、设计评审、「为什么不干脆…」式反问 | 双语 Q&A + Mermaid 图 |
| [架构决策记录 ADR](#架构决策记录-adr) | 任何要改动磁盘/内存契约的人 | ADR-001 … ADR-005 |
| [基准测试 Benchmarks](#基准测试-benchmarks) | 性能调优、回归排查 | 可运行压测脚本 |
| [实测报告 Reports](#实测报告-reports) | 容量规划、「放大到 10 倍还撑得住吗」 | 具名机器上的实测运行 |
| [实践配方 Recipes](#实践配方-recipes) | 用这个引擎**去解决**某个问题 | 可运行模式 + 成本表 |
| [源码地图 Source map](#源码地图-source-map) | 从概念反查代码 | 模块对照表 |

## 工程实践 Skills（底层实现指南）

三篇深入指南，每一篇都基于真实源码，而非泛泛而谈。

- [WAL 与 torn-tail 截断恢复](skills/wal-and-recovery.md) — 21 字节帧结构拆解、为什么
  CRC32 放在帧*最前*、五情形恢复决策表、可丢弃的尾部半帧与致命的流中段校验失败之间的
  区分，以及一个可被字节镜像 / `monkeypatch` / SIGKILL 三种测试共同驱动的 WAL 状态机。
- [原子落盘](skills/atomic-flush.md) — `临时文件 → fsync → rename → fsync(父目录)`，
  为什么跳过目录 fsync 会让数据「持久但无名」、`os.replace` 与 `os.rename` 的区别，以及
  Windows/NTFS 上的优雅降级。
- [并发控制](skills/concurrency-control.md) — 引用计数文件租约对比读写锁、快照序列带
  （snapshot sequence bands）、三把锁的集合与加锁顺序，以及基于 `threading.Barrier`
  的确定性竞态测试。

## 硬核问答 FAQ（存储系统深度追问）

双语、锚定代码、配图解析。八个问题覆盖 WAL/恢复、原子落盘、并发/Compaction、墓碑/
MemTable 快照、Bloom Filter，每篇内含四张 Mermaid 图。

- English: [Storage Engine Deep-Dive Q&A](faq/interview-qa-en.md)
- 简体中文: [存储引擎硬核追问与深度解答](faq/interview-qa-zh.md)

## 架构决策记录 ADR

五个塑造了引擎的关键决策，均以 **状态 / 背景 / 决策 / 后果** 格式记录。在改动任何磁盘
或内存契约之前，请先读这些。

- [ADR-001: WAL 帧完整性使用 CRC32](decisions/adr-001-wal-frame-crc32.md) — 为什么是
  CRC32 而非 SHA-256 或 CRC64。
- [ADR-002: Compaction 显式触发，而非后台调度](decisions/adr-002-manual-compaction-policy.md) —
  为什么拒绝后台 Compactor 线程。
- [ADR-003: 用引用计数文件租约保障快照安全](decisions/adr-003-refcount-file-snapshots.md) —
  为什么文件租约胜过 RWLock、epoch 与 finaliser。
- [ADR-004: 默认同步刷写 WAL（先写日志后应用）](decisions/adr-004-sync-flush-guarantees.md) —
  为什么每条已确认写入都立即 `fsync`。
- [ADR-005: 每个 SSTable 尾部使用稀疏块级索引](decisions/adr-005-sparse-index-layout.md) —
  为什么索引是稀疏、按块、置于尾部而非头部。

## 基准测试 Benchmarks

四者均可复现且自带断言：运行干净则退出码 `0` 并打印结论；不变量被违反则直接抛错，而不是
给出误导性的数字。

- [Compaction 下的并发读写压测](../benchmarks/bench_concurrent.py) —
  `python -m benchmarks.bench_concurrent`。多线程 `get`/`scan` 与后台 Compactor 并发竞争，
  汇报 P95/P99/吞吐，并断言没有读操作抛异常。
- [WAL 吞吐上限](../benchmarks/bench_wal_throughput.py) —
  `python -m benchmarks.bench_wal_throughput`。持久 / 分组 / 非安全三种写入，对照真实磁盘
  `fsync` 实测。
- [读放大矩阵](../benchmarks/bench_read_amplification.py) —
  `python -m benchmarks.bench_read_amplification`。两种文件布局 × 过滤器开关 × 命中/缺席，
  统计**每次逻辑点读对应的物理块读次数**，取自各 `SSTableReader` 自己的计数器而非估算。
- [核心引擎基准](../benchmarks/bench_engine.py) —
  `python -m benchmarks.bench_engine --markdown`。主文档中引用的核心数字即来自此脚本。

## 实测报告 Reports

压测负责测量；报告负责给出**对数字的解读**，连同采集它的机器、以及它刻意不覆盖的部分。

- [预发版性能基准报告](reports/benchmark-2024.md) — 写路径（持久化 `198–219 rec/s`，关闭
  `fsync` 为 `102,398–117,160 rec/s`）、读路径（命中 p50 `38.7–41.4 µs`，缺席 `8.2–9.0 µs`）、
  写放大 `3.54x` 拆成 WAL / flush / merge 三段、空间放大「每代 `1.17x`」与「一次未 compaction
  的全量覆盖后 `2.35x`」，以及为什么六个读线程只跑出单线程的 `0.98x`。每个数字都来自仓库内
  已提交的脚本；跨运行会漂移的量，报告发布区间而非单点。

## 实践配方 Recipes

引擎是底座，不是成品。这些页面拿一个引擎**没有**解决的问题，用引擎真正暴露的两个原语
——有序范围扫描与墓碑——把它搭出来。

- [用户态 TTL 淘汰](recipes/ttl-eviction.md) — `sable` 没有过期线程，也没有 compaction
  filter，于是过期能力建在一个**大端定长**的索引键上：它让「所有早于此刻过期的记录」精确等于
  一个半开区间查询。含三段可运行代码（`put_with_ttl`、`get_if_live`、`evict_expired`）、
  墓碑经过六个阶段时各自新增的读/写/空间代价，以及防止重写记录继承旧截止时间的
  generation guard 规则。

## 源码地图 Source map

| 模块 | 职责 | 关键符号 |
| --- | --- | --- |
| [`sable/types.py`](../src/sable/types.py) | 值类型、容量阈值、`SSTableMeta` | `InternalEntry`、`KeyRange`、`SSTableMeta` |
| [`sable/errors.py`](../src/sable/errors.py) | 损坏 / 半帧 / 调用方误用 的分类 | `CorruptionError`、`ChecksumMismatchError` |
| [`sable/fsutil.py`](../src/sable/fsutil.py) | 原子落盘原语 | `write_atomically` |
| [`sable/wal.py`](../src/sable/wal.py) | 帧编解码、torn-tail 扫描 | `WalReader`、`WalWriter` |
| [`sable/memtable.py`](../src/sable/memtable.py) | 跳表，`(key, -sequence)` 排序 | `MemTable` |
| [`sable/sstable.py`](../src/sable/sstable.py) | 数据块、索引、Bloom、Footer、位置读取 | `SSTableReader`、`SSTableWriter` |
| [`sable/bloom.py`](../src/sable/bloom.py) | 双哈希过滤器 | `BloomFilter` |
| [`sable/compaction.py`](../src/sable/compaction.py) | 分层调度、墓碑物理回收 | `Planner` |
| [`sable/engine.py`](../src/sable/engine.py) | 版本集、MANIFEST、读路径、租约 | `Engine`、`Snapshot` |
| [`sable/cli.py`](../src/sable/cli.py) | `put` / `get` / `delete` / `scan` / `stats` | `main` |

## 推荐阅读顺序

给初次接触代码库的人一条建议路径：

1. [`README.md`](../README.md) — 一页纸心智模型与图示。
2. [Skills → WAL](skills/wal-and-recovery.md) 然后 [Skills → 原子落盘](skills/atomic-flush.md) —
   其余所有保证都建立在这两条原语之上。
3. [Skills → 并发控制](skills/concurrency-control.md) — 这是大多数 LSM 实现都会写错的地方。
4. [ADR-001](decisions/adr-001-wal-frame-crc32.md) 到
   [ADR-005](decisions/adr-005-sparse-index-layout.md) — 你即将触碰的契约背后的推理。
5. [FAQ（中文）](faq/interview-qa-zh.md) — 「为什么不干脆…」式的反问，这里已经预先回答。

---

[← 返回项目主文档](../README.md)
