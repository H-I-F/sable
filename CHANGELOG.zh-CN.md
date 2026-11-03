[English](./CHANGELOG.md) | 简体中文

# 变更日志

`sable` 所有值得记一笔的变更都在这里。项目遵循
[语义化版本](https://semver.org/spec/v2.0.0.html)；在引擎到 1.0 之前，每个版本都可能改动布局或
API，这一点在这里写明，而不是让开发者在恢复数据时才撞上。格式沿用
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/)。

## [未发布]

### 新增

- **确定性的 PR 评审报告。** 每个 PR 现在会收到一条固定评论，含变更地图、实时 CI 门禁状态和
  针对本仓库的严谨性提示（快照/WAL 恢复语义、`requires-python` 与 CI 矩阵/镜像基础版本是否同步、
  分支保护同步）。顺带打上 `area/`、`size/`、`risk/` 标签。只提建议：不合并、不关闭、不批准。
  不做模型推理、不连外部服务、没有运行成本。

### 变更

- **仓库归属迁到 [H.I.F](https://github.com/H-I-F) 组织。** 规范地址现在是
  `https://github.com/H-I-F/sable`；原 `MKA930/sable` 路径仍能解析，因为 GitHub 会重定向迁移过的
  仓库。文档、打包元数据和容器标签里的每一个 URL 都改了，`PROGRESS.md` 里的历史 issue/PR 引用
  现在也落在新 owner 下。作者元数据未变。

  这是一次治理动作，不是发版：代码、存储布局、版本号都没动，已有安装和已有数据库不受影响。

## [0.1.1] - 2026-10-30

针对快照读的正确性修复：某个键被覆盖、而新旧版本仍同时存在于活跃 memtable 时，被钉住的视图
做的点查会出问题。

### 修复

- **快照点查现在解析到最新的*可见*版本。** 当一个键同时持有较新的、晚于快照的版本，以及视图
  仍有权读取的较旧版本时，`engine.snapshot().get(key)` 此前会返回「不存在」——memtable 查找只看
  最新版本，发现它不可见就放弃了，哪怕同一张表里就躺着一个可见的旧版本。范围扫描本来就会按
  可见性过滤，于是它和点查对一个固定 sequence 该解析出什么给出了不同答案。`memtable.get_visible`
  现在沿版本链走，返回第一个不高于视图 sequence 的版本，与 SSTable reader 一直正确的行为对齐。
- **`Engine.open_snapshots`** 暴露未释放视图的数量，调用方无需伸手进引擎内部就能断言一次
  acquire/release 配平为零。
- 修复了下游项目 sable-vector 开发期间发现的一个并发问题。

## [0.1.0] - 2026-10-30

第一个完整版本：一台单机上的完整 LSM-tree，每一条持久性决策都写成文件系统不变量。

### 新增

- **包骨架与领域契约** —— `sable.types` 固定词汇表（`InternalEntry`、`KeyRange`、`SSTableMeta`、
  选项块），`sable.errors` 固定故障分类，于是损坏、半帧写入、调用方误用不会混成一团。附带
  `py.typed`。
- **预写日志** —— 自描述的 21 字节帧（`crc32 | sequence | op | klen | vlen`），CRC32 覆盖校验和
  自身之后的每个字节；torn-tail 恢复能区分「可丢弃的尾部半帧」与「存活帧里的坏 CRC（致命）」。
  `truncate_torn_tail` 原地修复段，因此恢复是幂等的。
- **MemTable** —— 按 `(user_key, -sequence)` 排序的确定性跳表，让「新版本在前」成为结构性质而
  非约定。插入会拒绝 sequence 倒退，而不是悄悄破坏 read-your-writes。遍历与连表串行化，并发
  reader 不会看到连了一半的塔。
- **SSTable** —— 按重启点对齐的数据块、每块 CRC32、每块一行稀疏索引、Bloom filter 块，以及一个
  复核头部 CRC 的固定 footer。通过 `temp -> fsync -> rename -> fsync(parent-dir)` 发布，因此一张表
  要么完整，要么不存在。
- **读路径** —— 活跃 memtable，然后 immutables，然后 L0 从新到旧，再 L1..Ln。遍历在墓碑处*停下*：
  这是它遮住更老版本的方式，而不是跳过一个了事。
- **写路径** —— 先写日志后应用。分配 sequence、fsync 帧，之后条目才可见——这正是「确认」意味着
  「持久」的原因。
- **CLI** —— `sable put | get | delete | scan | stats`，含层级分布、每层字节数和估计的读放大。
- **范围扫描** —— 稀疏索引 seek 加多层 k 路归并；墓碑在归并里被消耗掉，因此被删的键是缺席，
  而不是带着哨兵值被 yield 出来。
- **Compaction** —— 分层调度器（L0 文件数，再按每层字节预算）物理回收被取代的版本，并在最深的、
  已经没有东西需要它遮掩的那一层丢掉墓碑。
- **快照** —— `Engine.snapshot()` 返回一个按 sequence 钉住的视图，O(1) 获取、不复制任何东西。
  文件由引用计数保护，版本由按每个视图可见带划分的保留规则保护。Compaction 拒绝丢弃存活视图
  仍可能读到的东西。
- **崩溃恢复测试矩阵** —— 十个 `SIGKILL` 注入点，逐一审计重开的存储与它确认过的写入是否完全一致。
- **基准套件** —— `python -m benchmarks.bench_engine` 测量持久写、memtable 发布、点查命中/缺席、
  全扫描、compaction 和重开成本，并输出 README 里嵌的那张 Markdown 表。
- **容器镜像** —— 多阶段构建，产出只含 wheel 的运行时，非特权运行、数据放卷上。
- **存储检查器** —— 三个只读 CLI，在**不打开**存储库的前提下回答关于它的问题，因此能拿来对付
  失败运行留下的残骸。`python -m sable.inspector` 逐帧审计 WAL、区分可丢弃的 torn tail 与致命的
  流中段校验失败；`python -m sable.sstable_inspector` 解出表头、稀疏索引与 Bloom 几何，并把过滤器
  填充率反解成条目数和误判率；`python -m sable.topology` 打印哪一层哪个文件持有哪些键范围，含
  活跃与被钉住的不可变版本。
- **故障注入套件** —— `tests/test_chaos_enospc.py` 驱动一个拒绝所有写入的 WAL 和一个 `fsync` 失败
  的 flush，断言被拒绝的写入在重开后不留下可见键、半截 `.sst`、`.tmp-sable` 或幻影键。
  `tests/test_chaos_fd_leak.py` 断言描述符数在 close 后回到基线、在混合写/flush/compact 流量下
  保持有界，并在三个读线程与 compactor 竞速时依然成立。
- **读放大套件** —— `python -m benchmarks.bench_read_amplification` 报告每次逻辑点读对应的物理块读
  次数，覆盖两种文件布局（六个重叠的 L0 文件对一个归并后的 run）与过滤器开/关，数字取自各 reader
  自己的 `data_block_reads` 计数器而非估算。
- **TTL 淘汰配方** —— `docs/recipes/ttl-eviction.md` 用引擎暴露的两个原语（有序范围扫描与墓碑）
  搭出过期能力，因为引擎没有过期线程也没有 compaction filter；并给它造出的六阶段墓碑生命周期
  逐段算清代价。
- **预发版性能报告** —— `docs/reports/benchmark-2024.md` 公布在一台具名机器上实测的写路径、读路径
  和三个放大系数，含多次运行之间的漂移，以及一份明确的「不覆盖什么」清单。
- **贡献者指南与 Makefile** —— `CONTRIBUTING.md` 写明四道门禁和工具强制的约定；
  `make test | lint | typecheck | chaos | clean` 包的就是 CI 跑的那几条命令，于是「本地过了」和
  「CI 过了」是同一件事。

### 变更

- Flush 在全局引擎锁之外发布 SSTable；替换表被挂上之前，旧表一直留在不可变集合里可见，因此
  没有 reader 能观察到「memtable 没了」到「表出现了」之间的空隙。
- Compaction 归并且完全不加引擎锁——输入不可变、输出在安装前无名——于是维护与真实工作重叠，
  而不是让它俩互相堵住。

### 安全

- 任何 token、secret、凭据都不会写进源文件、提交或测试夹具；崩溃套件让解释器跑测试模块里内嵌的
  脚本，不从外部取任何东西。

### 修复

- 两个检查器 CLI 对「路径不存在」的处理恰好相反：`sstable_inspector` 抛带栈的
  `FileNotFoundError`，`inspector` 却打印「0 frame(s)」并以 0 退出，于是管道分不清「空段」和
  「段没了」。现在两者都以退出码 2 在 stderr 给一行原因，SSTable CLI 也把外来或损坏文件走同一条
  路径。库函数不变——`inspect_wal` 对缺失文件仍返回 `[]`，这是段遍历需要的语义。

[0.1.0]: https://github.com/H-I-F/sable/releases/tag/v0.1.0
