# Implementation Progress

Single-step file-driven state machine. Every box is ticked only after the
PR-scoped acceptance command passes locally **and** the PR is merged.

- [x] PR 01: 包骨架 + 领域类型与错误契约
- [x] PR 02: WAL 帧格式、CRC32 校验与 torn-tail 截断恢复
- [x] PR 03: MemTable 排序与 Tombstone 遮蔽语义
- [x] PR 04: SSTable 二进制格式与原子落盘
- [x] PR 05: 读路径：MemTable → 不可变 MemTable → L0...Ln
- [x] PR 06: 写路径：append-before-apply 与刷盘持久化
- [x] PR 07: 生产级 CLI 交互入口 (sable put/get/stats)
- [x] PR 08: SSTable Bloom Filter 块与快速剪枝
- [x] PR 09: 稀疏索引驱动的 Range Scan 与 Multi-Level K-Way Merge
- [x] PR 10: Compaction 机制与 Tombstone 物理回收
- [x] PR 11: 引用计数保护的快照并发安全 (threading.Barrier)
- [x] PR 12: SIGKILL 进程崩溃恢复矩阵测试
- [x] PR 13: 变更日志与演示用例 (examples/crash_demo.py)
- [x] PR 14: GitHub Actions 生产级 CI 检查管线
- [x] PR 15: 容器化多阶段 Dockerfile 与双语硬核架构 README
- [x] PR 16: 基准测试套件与最终性能数据归档

## Phase 2 — Engineering Knowledge Base (PR 17 ~ PR 21)

- [x] PR 17: docs(skills): 沉淀存储引擎底层核心工程实践指南
- [x] PR 18: docs(faq): 双语硬核存储系统高频技术追问与深度解答
- [x] PR 19: docs(adr): 落地工业级架构决策记录 (ADR-001 ~ ADR-005)
- [x] PR 20: bench(stress): 增加多线程并发读写与高竞争压测基准
- [x] PR 21: docs(portal): 顶流双语主文档重构与交互式架构索引中枢

## Phase 3 — Pre-Release Hardening & Governance (PR 22 ~ PR 31)

> **编号对照**：下表用的是**计划编号**（PR 22 起），实际 GitHub PR 号整体后移一位
> （计划 PR 22 → 实际 #23），因为 Phase 2 的 PR #22 已经占用了那个号。两者一一对应，
> 合并顺序与下表顺序完全一致。

- [x] PR 22: feat(inspect): WAL 二进制帧可视化检查器 → [#23]
- [x] PR 23: feat(inspect): SSTable 块与 Bloom Filter 解析器 → [#24]
- [x] PR 24: feat(inspect): 引擎文件版本拓扑与层级视图 → [#25]
- [x] PR 25: test(chaos): 磁盘空间耗尽 (ENOSPC) 故障注入测试 → [#26]
- [x] PR 26: test(chaos): 文件描述符泄漏探测与并发熔断测试 → [#27]
- [x] PR 27: docs(recipes): 基于 Tombstone 的用户态 TTL 淘汰实践 → [#28]
- [x] PR 28: bench(read-amp): 读放大 (Read Amplification) 统计压测脚本 → [#29]
- [x] PR 29: docs(reports): 基于真实压测数据的预发版性能基准报告 → [#30]
- [x] PR 30: docs(governance): 开源贡献指南 CONTRIBUTING.md → [#31]
- [x] PR 31: build(meta): 统一本地开发与测试命令的 Makefile → [#32]
- [x] PR 32: fix(inspect): 检查器 CLI 在文件缺失或损坏时以退出码 2 报错，而非打印空表或抛栈 → [#33]

> PR 32 不在原计划的 10 项之内：它是 PR 30（贡献指南）逐条实跑文档命令时发现的缺陷 ——
> `python -m sable.sstable_inspector <不存在的路径>` 会抛 `FileNotFoundError` 栈，
> 而 `python -m sable.inspector <不存在的路径>` 会打印「0 frame(s)」并以 0 退出，
> 于是管道无法区分「文件为空」与「文件不存在」。两条路径都已修正并有测试覆盖。

## Phase 4 — Release Prep

收尾阶段。第一项能进版本库，其余三项是仓库侧操作，不进提交。

| 项 | 状态 |
| --- | --- |
| `docs(index)`：把 recipe 与 report 接进四处入口（中英双语），`docs(changelog)` 把加固期的工作并入 `[0.1.0]` 并补 `### Fixed` | 本 PR |
| 打 `v0.1.0` tag 并创建 GitHub Release —— 消除 CHANGELOG 里指向不存在 tag 的死链 | 合并后执行 |
| 清理 19 个远端 / 19 个本地特性分支（`git cherry` 未合并补丁数为 0），并开启 `delete_branch_on_merge` | 合并后执行 |
| 仓库治理：补 topics，为 `main` 开启分支保护（要求 PR + CI 通过） | 合并后执行 |

[#23]: https://github.com/H-I-F/sable/pull/23
[#24]: https://github.com/H-I-F/sable/pull/24
[#25]: https://github.com/H-I-F/sable/pull/25
[#26]: https://github.com/H-I-F/sable/pull/26
[#27]: https://github.com/H-I-F/sable/pull/27
[#28]: https://github.com/H-I-F/sable/pull/28
[#29]: https://github.com/H-I-F/sable/pull/29
[#30]: https://github.com/H-I-F/sable/pull/30
[#31]: https://github.com/H-I-F/sable/pull/31
[#32]: https://github.com/H-I-F/sable/pull/32
[#33]: https://github.com/H-I-F/sable/pull/33
