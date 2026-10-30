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
