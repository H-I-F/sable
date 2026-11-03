[English](adr-003-refcount-file-snapshots.md) | [简体中文](adr-003-refcount-file-snapshots.zh-CN.md)

# ADR-003：用引用计数文件租约保障快照安全

**状态：** 已接受 —— PR 11 实现。

**日期：** 2026-10-26

**决策人：** 引擎维护者

---

## 背景

Compaction 读一组 SSTable、归并、发布输出、删掉输入。而一个并发读可能正握着其中某个输入文件
打开的 reader。

这是两种不同的危险，需要两种不同的机制：

1. **生命周期。** reader 还需要这个文件时它被 unlink 了。POSIX 上，握着已打开 fd 的 reader 仍能
   读那个被 unlink 的 inode，于是 bug 会一直藏着，直到*另一个* reader 去打开路径、拿到
   `FileNotFoundError` —— 不确定性，且概率随读并发长大。Windows 上 `unlink` 自己就抛
   `PermissionError`。
2. **可见性。** 即便文件还在，归并也可能丢掉一个存活快照仍有权读取的版本。

本 ADR 管危险 1。危险 2 由 `compaction.visible_versions` 里的序列带处理，刻意是另一套机制。

要求：reader 必须能在脚下这棵树正被重写的情况下跑完，而 compaction 不必等 reader 结束。

---

## 决策

`SSTableMeta` 带一个 `refs` 计数器。每次读在开跑之前，通过一个 context manager 对整个文件集
取得**租约**：

```python
with self._lock:
    tables = [self._active, *self._immutable]
    metas = [meta for level in self._levels for meta in level]
    for meta in metas:
        meta.pin()
```

租约在 `finally` 里释放，包括生成器迭代中途被丢弃、由 GC 关闭的情形。

Compaction 立刻把输入从版本集里拿掉，但把物理 unlink 往后推：

```python
if meta.refs > 0:
    self._pinned_obsolete.append(meta)
    continue
(home / meta.file_name).unlink(missing_ok=True)
```

`_pinned_obsolete` 就是那块墓地。最后一个 unpin 的 reader 触发 `_discard_retired_files`，把
`refs` 归零的文件全部 unlink，并返回列表，好让所在目录整批只 fsync 一次。

那条不显然的要求：**抓取视图和递增 `refs` 必须彼此原子。** 拆开，竞态就回来了：

```
reader:  resolve metas           compaction: drop meta from _levels
reader:  (还没 pin)              compaction: 看到 refs == 0 -> unlink
reader:  pin()  -> 太晚了
```

---

## 考虑过的备选

### 在文件集外面套读写锁

否决。RWLock 也能挡住 unlink，但它把 compaction 的逻辑效果和物理效果混成了一件事。握住写侧意味着
compaction 要等所有在飞的 reader，于是一次长 scan 直接拖住维护。引用计数让*逻辑*效果立刻落地
——文件马上离开版本集——只把*物理*效果延后。

它对读的形态也更不合适：一次 scan 在整个持续期内需要一套连贯的文件，在 RWLock 下意味着把这
段时间一直握着读侧。在计数下它只是一份租约，每个文件一个整数，完全不等待。

### 基于 epoch 的回收（hazard pointer）

对这个场景属于杀鸡用牛刀，否决。epoch 回收的高光场景是回收者绝不能阻塞、且 reader 集合又大又频繁变动。
这里 reader 集合被线程数框住，回收工作只是一次 `unlink`。多出来的簿记会引入一份必须被周期性
扫描的全局状态——那又是个调度器，而 ADR-002 正是为了避开它才存在的。

### 靠 GC / finaliser

否决。reader 对象被回收时才删文件，等于让删除时机变成垃圾回收器的函数，而 CPython、PyPy 和未来
的 CPython 版本各不相同。这没法测：测试不能断言一个文件*何时*消失。显式租约让生命周期可观察，
且与解释器无关。

### 每次读复制一份文件集

否决。那会让读成本正比于树大小而不是实际读到的数据，把分层树的意义抹掉了。

### compaction 期间绝不删，`close()` 时统一扫

否决。空间就被进程寿命而非数据框住，一个长时间跑的存储会无限涨。

---

## 理由

引用计数把*独占访问*换成了*延迟回收*，后者是更弱、也更恰当的保证。Compaction 从不等 reader，
reader 从不等待 compaction，两者只通过一个双方本来都要看的整数打交道。

这个设计还把临界区压得很小。`_lock` 只握着一个列表推导加一轮整数自增，不是握着整次 scan。
正确性要求不是「把锁握久一点」，而是「让这两步彼此原子」——一个弱得多、也便宜得多的条件。

---

## 后果

**正面**

- Compaction 的逻辑结果立刻生效；只有字节回收要等。
- Reader 不阻塞，也不阻塞 writer。
- 一次长 scan 拖不住维护，维护也拖不住 scan。
- 删除是批量的，于是目录每批 fsync 一次，而不是每个文件一次。
- 文件的生命周期可观察：`meta.refs` 能查，
  `tests/test_concurrent.py::test_retired_file_outlives_the_walk_that_leased_it` 断言的是可观察的
  后果，不是一个内部计数器。

**负面**

- 两套机制而不是一套。文件 pin 不解决版本可见性，`visible_versions(snapshots=...)` 才管。将来
  有人修好一边就想当然地以为另一边也好了，这是个潜在 bug。
- 一个永不释放租约的 reader —— 生成器被存到某处又丢在那儿 —— 会无限期钉住字节。租约由
  `contextlib` 持有，正是为了让生成器 close 时释放；但调用方把迭代器一直留着，文件就一直活着。
- 回收不及时：被删文件的字节要撑到最后一个租约落下，所以磁盘占用可能短暂超过树的逻辑大小。
- `unpin()` 在释放不平衡时抛异常，而不是默默容忍。这是刻意的——重复释放说明租约簿记错了——
  但它会让调用方的 bug 很响。

**运维提示**

因为回收被延后，树上的 `stats` 类报告里可能出现已经不在版本集里的文件。这是预期行为：版本集
是读去查的东西，墓地是文件系统还拿着的东西。
