[English](RELEASE_NOTES_v0.1.0.md) | [简体中文](RELEASE_NOTES_v0.1.0.zh-CN.md)

# sable v0.1.0

`sable` 是一台单机上的、崩溃安全的纯 Python LSM-tree 键值存储，标准库之外零运行时依赖。

这个版本里每一条持久性声明，都是一条你可以审计的文件系统不变量：21 字节自描述 WAL 帧，
CRC32 覆盖校验和之后的每个字节；SSTable 通过
`temp -> fsync -> rename -> fsync(parent)` 发布；以及一份引用计数租约，让正被某个快照指着的
文件在 compaction 期间仍然可读。

```bash
pip install "sable @ git+https://github.com/MKA930/sable@v0.1.0"
python -c "import sable; print(sable.__version__)"   # 0.1.0
```

别信这些保证，去验它们：

```bash
make test        # 181 个测试，94.16% 语句覆盖率（下限 85%）
make chaos       # SIGKILL 矩阵、ENOSPC 注入、描述符泄漏探测
make lint typecheck
```

---

第一个完整版本：一台单机上的完整 LSM-tree，每一条持久性决策都写成文件系统不变量。

### 新增

- **包骨架与领域契约** —— `sable.types` 固定词汇表（`InternalEntry`、`KeyRange`、
  `SSTableMeta`、选项块），`sable.errors` 固定故障分类，于是损坏、半帧写入、调用方误用不会
  混成一团。附带 `py.typed`。
- **预写日志** —— 自描述的 21 字节帧（`crc32 | sequence | op | klen | vlen`），CRC32 覆盖
  校验和自身之后的每个字节；torn-tail 恢复区分「可丢弃的尾部半帧」与「存活帧里的坏 CRC
  （致命）」。`truncate_torn_tail` 原地修复段，因此恢复是幂等的。
- **MemTable** —— 按 `(user_key, -sequence)` 排序的确定性跳表，让「新版本在前」成为结构性质。
  插入拒绝 sequence 倒退，而不是悄悄破坏 read-your-writes。遍历与连表串行化，并发 reader
  不会看到连了一半的塔。
- **SSTable** —— 按重启点对齐的数据块、每块 CRC32、每块一行稀疏索引、Bloom filter 块，
  以及复核头部 CRC 的固定 footer。通过 `temp -> fsync -> rename -> fsync(parent-dir)` 发布，
  因此一张表要么完整，要么不存在。
- **读路径** —— 活跃 memtable，然后 immutables，然后 L0 从新到旧，再 L1..Ln。遍历在墓碑处
  停下，这是它遮住更老版本的方式。
- **写路径** —— 先写日志后应用。分配 sequence、fsync 帧，之后条目才可见——这正是「确认」
  意味着「持久」的原因。
- **CLI** —— `sable put | get | delete | scan | stats`，含层级分布、每层字节数和估计的读放大。
- **范围扫描** —— 稀疏索引 seek 加多层 k 路归并；墓碑在归并里被消耗掉，被删的键是缺席，
  而不是带着哨兵值被 yield 出来。
- **Compaction** —— 分层调度器（L0 文件数，再按每层字节预算）物理回收被取代的版本，并在
  最深的、已经没有东西需要它遮掩的那一层丢掉墓碑。
- **快照** —— `Engine.snapshot()` 返回一个按 sequence 钉住的视图，O(1) 获取、不复制任何
  东西。文件由引用计数保护，版本由按每个视图可见带划分的保留规则保护。Compaction 拒绝丢弃
  存活视图仍可能读到的东西。
- **崩溃恢复测试矩阵** —— 十个 `SIGKILL` 注入点，逐一审计重开的存储与它确认过的写入是否
  完全一致。
- **基准套件** —— `python -m benchmarks.bench_engine` 测量持久写、memtable 发布、点查命中 /
  缺席、全扫描、compaction 和重开成本，并输出 README 里嵌的那张 Markdown 表。
- **容器镜像** —— 多阶段构建，产出只含 wheel 的运行时，非特权运行、数据放卷上。
- **存储检查器** —— 三个只读 CLI，在**不打开**存储库的前提下回答关于它的问题。
  `python -m sable.inspector` 逐帧审计 WAL、区分可丢弃的 torn tail 与致命的流中段校验失败；
  `python -m sable.sstable_inspector` 解出表头、稀疏索引与 Bloom 几何，并把过滤器填充率反解成
  条目数和误判率；`python -m sable.topology` 打印哪一层哪个文件持有哪些键范围，含活跃与被
  钉住的不可变版本。
- **故障注入套件** —— `tests/test_chaos_enospc.py` 驱动一个拒绝所有写入的 WAL 和一个
  `fsync` 失败的 flush，断言被拒绝的写入在重开后不留下可见键、半截 `.sst`、`.tmp-sable`
  或幻影键。`tests/test_chaos_fd_leak.py` 断言描述符数在 close 后回到基线、在混合
  写/flush/compact 流量下保持有界，并在三个读线程与 compactor 竞速时依然成立。
- **读放大套件** —— `python -m benchmarks.bench_read_amplification` 报告每次逻辑点读对应的
  物理块读次数，覆盖两种文件布局（六个重叠 L0 文件对一个归并后的 run）与过滤器开/关，取自
  各 reader 自己的 `data_block_reads` 计数器而非估算。
- **TTL 淘汰配方** —— `docs/recipes/ttl-eviction.md` 用引擎暴露的两个原语（有序范围扫描与
  墓碑）搭出过期能力，因为引擎没有过期线程也没有 compaction filter；并给它造出的六阶段
  墓碑生命周期逐段算清代价。
- **预发版性能报告** —— `docs/reports/benchmark-2024.md` 公布在一台具名机器上实测的写路径、
  读路径和三个放大系数，含多次运行之间的漂移，以及一份明确的「不覆盖什么」清单。
- **贡献者指南与 Makefile** —— `CONTRIBUTING.md` 写明四道门禁和工具强制的约定；
  `make test | lint | typecheck | chaos | clean` 包的就是 CI 跑的那几条命令。

### 变更

- Flush 在全局引擎锁之外发布 SSTable；替换表被挂上之前，旧表一直留在不可变集合里可见，
  因此没有 reader 能观察到「memtable 没了」到「表出现了」之间的空隙。
- Compaction 归并完全不加引擎锁——输入不可变、输出在安装前无名——于是维护与真实工作重叠，
  而不是让它俩互相堵住。

### 安全

- 任何 token、secret、凭据都不会写进源文件、提交或测试夹具；崩溃套件让解释器跑测试模块里
  内嵌的脚本，不从外部取任何东西。

### 修复

- 两个检查器 CLI 对「路径不存在」的处理恰好相反：`sstable_inspector` 抛带栈的
  `FileNotFoundError`，`inspector` 却打印「0 frame(s)」并以 0 退出，于是管道分不清「空段」
  和「段没了」。现在两者都以退出码 2 在 stderr 给一行原因，SSTable CLI 也把外来或损坏文件
  走同一条路径。库函数不变——`inspect_wal` 对缺失文件仍返回 `[]`。

---

- 仓库：<https://github.com/H-I-F/sable>
- 发布页：<https://github.com/H-I-F/sable/releases/tag/v0.1.0>
- 完整历史：[CHANGELOG.md](../CHANGELOG.md)
- 工程笔记：[docs/README.md](README.zh-CN.md)
