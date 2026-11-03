[English](snapshot-read-consistency.md) | [简体中文](snapshot-read-consistency.zh-CN.md)

# 当 `get` 和 `scan` 对一个快照给出不同答案

一个从下游报上来的 bug，最后查出是引擎内部把同一个谓词实现了两遍。

## 一句话概括

| | |
| --- | --- |
| **症状** | 被钉住的 `Engine.snapshot()` 可能找不到一个已提交的键——只要它的最新版本晚于快照 |
| **报告方** | [`sable-vector`](https://github.com/H-I-F/sable-vector) 的并发读测试，当时它固定引用本仓库 |
| **根因** | `MemTable.get()` 只看版本链的链头，一发现不可见就放弃；范围扫描却会沿链走并按可见性过滤 |
| **修复** | `MemTable.get_visible()` —— 返回第一个不高于视图 sequence 的版本 |
| **发布为** | [`v0.1.1`](https://github.com/H-I-F/sable/releases/tag/v0.1.1) |

## 它是怎么来的

sable-vector 套件里有两个测试每次都挂。一个是表面问题——引用了一个引擎没暴露的可观测属性。
另一个不是：并发 reader 看到了一条已提交记录的 payload，它的向量却不见了。由于一条向量记录是
分几个键暂存的，这个症状既可能是下游的分段写写坏了，也可能是这里的可见性规则坏了。

有用的那一步，是把行为放到完全没有下游代码的环境里复现：

```python
# put K=A  -> sequence 1
# put K=B  -> sequence 4（同一个键，更新的版本）
# 快照钉在 sequence 2
view.get(b"K")   # 期望 b"A"（不高于 2 的最新版本）
                 # 实际：None
```

三行纯 `sable`，附近没有任何向量代码。归属就此定了：这是引擎的缺陷，调用方再小心也绕不过去。

## 到底错在哪

memtable 是按 `(user_key, -sequence)` 排序的确定性跳表，所以一个键的最新版本排在它版本链的
链头。这让「新版本赢」成了结构性质。但引擎有两条读路径，它们没有实现同一条可见性规则：

- **范围扫描**（`scan` / `_versioned_scan`）走遍每个版本，按 `sequence <= visible_sequence`
  过滤，取第一个幸存者。对的。
- **点查**（`MemTable.get`）直奔链头，可见就返回，不可见就给 `None` —— 它停下，没有继续
  往链下面走。

`SSTableReader.get_entry` 本来就是对的，所以这个 bug 只在键还待在 memtable 里时出现：flush 或
compaction 之后，同样的读就正常了。它需要版本*共存*——而这正是并发 writer 会造出来的局面。
视图钉在 sequence 2、链头是个不可见的 sequence 4、后面跟着本该可见的 sequence 1 时，`get`
看见链头、判定不可见、停下、报成不存在；对同一个键做扫描却会返回 `A`。

两条读路径对「这个固定 sequence 该解析出什么」给出两个答案，不是一个微妙的性能问题。它就是
「坏了」的定义，而且让 `SECURITY.md` 里的快照保证失效。

## 修复

把 SSTable reader 早就有的规则教给 memtable：

```python
def get_visible(self, user_key, visible_sequence):
    with self._mutation:
        node = self._descend_to(user_key)
        while node is not None:
            candidate = node.entry
            if candidate.user_key != user_key:
                break
            if visible_sequence is None or candidate.sequence <= visible_sequence:
                return candidate
            node = node.next[0]
    return None
```

`Engine._lookup_entry` 现在对 memtable 改调 `table.get_visible(user_key, visible_sequence)`。
`visible_sequence is None` 仍然表示「最新者胜」，普通读没变。两个回归测试写在引擎级而不是
下游级，这样即便报告方哪天不在了，不变量仍有人守着。

另一个挂掉的测试想断言一次快照 acquire/release 配平为零。`Engine.open_snapshots` 现在暴露
未释放视图的数量；快照会钉住文件、留住版本，所以能不伸手进内部就检查这个配平，正是让一次
缓慢泄漏不至于在六周后变成悬案的东西。

**验证：** 纯 `sable` 复现通过，原本失败的两个下游测试转绿，本仓库套件保持 183 个测试、
五项 CI 检查全绿，sable-vector 保持 374 passing / 1 xpassed——那个 `xpassed` 正是以前被预期
为失败的并发回归用例。

## 为什么下游修不了

`sable` 没有原子多键 batch：每次 `put` 都拿到自己的 sequence。调用方暂存一条记录的各个部分时，
只能靠存储引擎的可见性规则让它们在 reader 眼里看起来是原子的。面对一个「碰到第一个不可见版本
就停」的 memtable，调用方没有任何 sequence 算术能绕开。诚实的选项只有两个：修引擎，或者在
文档里写明「并发快照读可能丢已提交的键」。

## 两个仓库之间怎么发出去

1. 在这里落地修复（PR #37），附两个引擎级回归测试。
2. 提到 **v0.1.1** 并打 tag——行为变了就得有版本号，CHANGELOG 里也点名了是哪个下游项目把
   它揪出来的。
3. 下游把固定版本从 `@main` 挪到已发布的 tag
   （`sable @ git+https://github.com/H-I-F/sable.git@v0.1.1`），让发布能被复现，而不是指着
   碰巧存在的那个提交。
4. 给这里的两份 README 加一节 `## Ecosystem`，指回下游的完整记录，让两边都能发现这层关系。

## 几点教训

- **同一个谓词实现两遍，早晚会被实现成两个样子。** 两条路径本该共同遵守的不变量是「不高于
  视图 sequence 的最大 sequence」。只有一边实现了，就给同一个问题造了两个答案。
- **结构不等于语义。** 按 `(user_key, -sequence)` 排序让最新版本排在前面，不等于让它可见。
  在有人来要一个既不是最新、也不是不存在的版本之前，那个捷径一直好使。
- **去依赖里复现。** 三行纯 `sable` 决定了该修哪个仓库。不这么做，最自然的猜测会是调用方的
  分段写有 bug。
- **100% 失败的测试是份礼物。** 别人已经替你写好了确定性复现；修掉它是去掉一笔长期的税。

## 链接

- [`v0.1.1` 发布页](https://github.com/H-I-F/sable/releases/tag/v0.1.1)
- [下游的完整记录](https://github.com/H-I-F/sable-vector/blob/main/docs/case-studies/cross-layer-bugfix.zh-CN.md)
- [`sable-vector`](https://github.com/H-I-F/sable-vector) —— 报告这个 bug 的项目
