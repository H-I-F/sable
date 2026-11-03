[English](./SECURITY.md) | 简体中文

# 安全策略

## 受支持的版本

sable 还没到 1.0，只有最新的 `0.1.x` 系列会收到安全修复。

| 版本 | 是否支持 |
| --- | --- |
| 0.1.x | ✅ |
| < 0.1 | ❌ |

像 [`sable-vector`](https://github.com/H-I-F/sable-vector) 这样的下游项目把 `sable` 固定到已发布的
tag，修复通过提升固定版本送达它们。

## 报告漏洞

安全问题**请不要**开公开 issue。

通过 [Security 页](https://github.com/H-I-F/sable/security/advisories/new) 的
"Report a vulnerability" 私下提交。

一般 72 小时内确认。尽量附最小复现：受影响的版本、操作序列（尤其是崩溃或并发访问步骤），以及
实际表现与预期表现。

## 范围

sable 是**嵌入式**存储引擎，跑在调用方进程里、信任宿主进程。除非另有说明，以下不在范围内：

- 客户端的认证 / 授权（本来就没有，它是个库）。
- 网络传输安全（宿主自己在上层搭的 RPC 由宿主负责）。
- 构建机的供应链完整性。

在范围内的是：

- **持久性与恢复**：WAL 重放、manifest 完整性，以及非正常关机或 `kill -9` 之后的行为。
- **快照隔离**：快照在整个生命周期内必须观察到一致的时间点，包括跨越 memtable 与 SSTable 边界。
- **持久化数据的完整性**：损坏检测、校验和处理，以及半写记录的恢复语义。
- **资源安全**：由精心构造的输入在引擎自身控制循环里触发的无界增长或耗尽。

## 对验证的期望

涉及持久性或隔离的报告，应当附一个在干净 checkout 上就会失败的复现——一段脚本加它的实际输出
最理想。历史上的例子：快照能看到一次它本不该看到的写入，因为 memtable 查找没有遵守快照钉住的
sequence。该问题已在 `get_visible()` 中修复并随 `v0.1.1` 发布，完整记录见
[sable-vector 的跨层复盘](https://github.com/H-I-F/sable-vector/blob/main/docs/case-studies/cross-layer-bugfix.zh-CN.md)。
