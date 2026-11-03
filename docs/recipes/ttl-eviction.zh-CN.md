[English](ttl-eviction.md) | [简体中文](ttl-eviction.zh-CN.md)

# 用键里编码的过期时间做应用层 TTL

`sable` 没有原生 TTL：没有后台过期线程，没有引擎替你强制的每键截止时间，也没有会悄悄丢掉旧数据
的 compaction filter。但它有两样东西，正是所有 TTL 方案的地基：

- **有序范围扫描。** `Engine.scan(start, end)` 按字节升序走键，所以只要把时间编进键里，键布局
  就能按时间检索。
- **墓碑。** `Engine.delete(key)` 追加一个删除标记；一次 compaction 把这个墓碑归并过所有更老的
  版本之后，键才算真的没了。

这份配方把 TTL 架在这两个原语之上，一行引擎代码都不改。当你今天就需要过期、又等不到内核特性时，
该拿起来的就是它；最后那节算清了它实际要付的账，因为「写个墓碑就行」把一张真实账单藏起来了。

## 键的格式

每条逻辑记录写两个键，放在同一个键空间里、用一个 tag 字节分开，于是一次扫描就能定位任一索引：

```
data key    : <namespace> 0x00 'd' <ident:16>
expiry key  : <namespace> 0x00 'x' <expires_at:uint64 BE> <ident:16>
value (data): <expires_at:uint64 BE> <payload>
```

过期时间戳是**大端、定长**的。这不是装饰：范围扫描比的是原始字节，所以 `struct.pack(">Q", t)`
是唯一一种在整个 64 位范围内「字节序等于数值序」的编码。换成小端，`256` 会排在 `1` 前面，
一次「找所有已过期的键」的扫描会悄悄漏掉键空间的一大半。

把时间戳编进 expiry key，正是让淘汰变成*范围*查询而不是全表扫描的原因：`now` 之前过期的一切，
恰好就是半开区间 `[ns 0x00 'x' 0 … ns 0x00 'x' (now + 1))`。

## 带 TTL 地写

data key 保持稳定、可按 `ident` 寻址，所以点查不需要知道截止时间也能工作。expiry key 替清扫
带着截止时间。

```python
import struct
import time

from sable import Engine, WriteOptions

SEP = b"\x00"
TAG_DATA = b"d"
TAG_EXPIRY = b"x"
ID_LEN = 16


def data_key(namespace: bytes, ident: bytes) -> bytes:
    if len(ident) != ID_LEN:
        raise ValueError(f"ident must be exactly {ID_LEN} bytes")
    return namespace + SEP + TAG_DATA + ident


def expiry_key(namespace: bytes, expires_at: int, ident: bytes) -> bytes:
    return namespace + SEP + TAG_EXPIRY + struct.pack(">Q", expires_at) + ident


def put_with_ttl(
    engine: Engine,
    namespace: bytes,
    ident: bytes,
    payload: bytes,
    ttl_seconds: int,
    *,
    now: int | None = None,
    sync: bool = True,
) -> int:
    """把 payload 存在 ident 下，并安排它在 ttl_seconds 之后过期。

    返回 Unix 时间戳形式的绝对过期时间。
    """
    now = int(time.time()) if now is None else now
    expires_at = now + ttl_seconds
    value = struct.pack(">Q", expires_at) + payload
    options = WriteOptions(sync=sync)

    engine.put(data_key(namespace, ident), value, options)
    # 过期索引存的是 (expires_at, ident)，这样同一个 ident 用新 TTL 重写时，
    # 就能把陈旧的行和仍然有效的行区分开。
    engine.put(
        expiry_key(namespace, expires_at, ident),
        struct.pack(">Q", expires_at) + ident,
        options,
    )
    return expires_at
```

每条记录写两次，是一个按时间排序的二级索引的价钱；另一种做法——每次清扫都扫整个命名空间——
是拿这份写放大去换一个随表大小无限涨的读成本。

## 防御式地读

淘汰是异步的，所以一个键可能已经过了截止时间、却还没被写墓碑。value 里带着截止时间正是为了让
reader 能在本地拒掉它，而不是把陈旧数据端出去：

```python
def get_if_live(
    engine: Engine, namespace: bytes, ident: bytes, *, now: int | None = None
) -> bytes | None:
    """只有在没过期时才返回 payload；过了截止时间返回 None。"""
    raw = engine.get(data_key(namespace, ident))
    if raw is None:
        return None
    expires_at = struct.unpack(">Q", raw[:8])[0]
    now = int(time.time()) if now is None else now
    if expires_at <= now:
        return None
    return raw[8:]
```

这让正确性与清扫器跑得多勤快解耦：清扫器负责回收空间，而「值还有效吗」由 reader 说了算。

## 淘汰已过期的键

清扫是一次范围扫描，接着一批墓碑写入。它刻意分**两阶段**：迭代时先收集受害者，迭代器耗尽之后
再删。扫描中途改 memtable，等于把活儿喂给正在产出活儿的那个迭代器自己。

```python
def evict_expired(
    engine: Engine,
    namespace: bytes,
    *,
    now: int | None = None,
    compact: bool = True,
) -> int:
    """给 now 之时或之前过期的每条记录写墓碑；返回删掉了多少行。"""
    now = int(time.time()) if now is None else now
    start = namespace + SEP + TAG_EXPIRY + struct.pack(">Q", 0)
    end = namespace + SEP + TAG_EXPIRY + struct.pack(">Q", now + 1)

    doomed_data: list[bytes] = []
    doomed_index: list[bytes] = []
    for index_key, blob in engine.scan(start, end):
        expires_at = struct.unpack(">Q", blob[:8])[0]
        ident = blob[8:]
        doomed_index.append(index_key)
        if len(ident) != ID_LEN:
            continue  # 畸形行：丢掉索引条目，绝不碰数据
        data = engine.get(data_key(namespace, ident))
        # 只有当*当前*截止时间正是我们要淘汰的那个，才删记录。
        # 后来用更晚 TTL 的重写必须能从一条陈旧的旧行手里活下来。
        if data is not None and struct.unpack(">Q", data[:8])[0] == expires_at:
            doomed_data.append(data_key(namespace, ident))

    # 批量、不带 sync 地删，然后刷一次：SSTable 发布会把整批 fsync，
    # 于是持久性只付一次，而不是每个墓碑付一次。
    options = WriteOptions(sync=False)
    for key in doomed_data:
        engine.delete(key, options)
    for key in doomed_index:
        engine.delete(key, options)

    if doomed_index:
        engine.flush()
        if compact:
            while engine.compact():
                pass
    return len(doomed_data)
```

用定时器跑它（cron、supervisor 循环、请求触发的 job 都行，引擎不关心）。因为 `sable` 里的
compaction 是显式的，清扫里的那一步 compaction 不是可选项：光删除只是把数据*标记*为死了。

## 一个墓碑到底要付什么

墓碑写起来便宜，退休起来贵。在这两个时刻之间，它占着空间、参与读，还可能活过好几次 compaction。
这套方案里一条过期记录的生命周期：

| 阶段 | 磁盘上有什么 | 代价 |
| --- | --- | --- |
| TTL 写入 | data 记录 + expiry 记录 | 2× 写、2× WAL 帧、2× fsync（除非批量） |
| 过期清扫 | 2 个墓碑追加进 WAL | 又 2 帧；原始记录还在盘上 |
| Memtable flush | 2 个墓碑落进一个 L0 文件 | 读路径现在要查它们 |
| Compaction L0 → L1 | 墓碑与更老版本归并 | 写放大：被重写的字节包含死数据 |
| Compaction 到底层 | 墓碑遇上最后一个更老版本 | **这时**字节才被物理丢掉 |
| 空间收回 | 记录消失 | 存储终于还回来了 |

由此得到三条结论，也正是生产里会咬人的三条：

1. **删除是一次写，不是一次减法。** 淘汰 `n` 条记录会追加 `2n` 个墓碑。一次清掉一百万个键的
   清扫，本身就是一次百万级记录的写入尖峰。批量处理（`sync=False`）、只 flush 一次、并把它
   排到热路径之外。
2. **墓碑只有在最底层才算「没了」。** 在 compaction 把它归并过所有更老版本之前它一直被保留，
   所以空间是*惰性*回收的。一个从不到达最底层的负载，永远收不回它「删掉」的空间——只是在
   攒墓碑。因此每次清扫后要把 `engine.compact()` 跑到它不再做事。
3. **重写需要 generation guard。** 同一个 `ident` 用新 TTL 再写一次，会留下旧的 expiry 行。清扫
   在删之前拿当前 value 的截止时间和该行的比对，于是旧行只能删掉它为之而生的那份数据，绝不会
   动到更新的版本。

## 运维提示

- **截止时间用单调钟，存储用墙钟。** 以 `time.time()` 存的截止时间跨重启是对的，但会随 NTP
  跳变。亚分钟级 TTL，在 value 里存由单调钟推出的截止时间、进程内用 `time.monotonic()` 比较，
  跨重启再退回墙钟。
- **给清扫设上界。** 一次扫过巨大的已过期区间，会同时握住很多 SSTable 的引用。用 `scan` 的
  `limit=` 分页，或者按固定时间窗（`now - 1h … now`）迭代，让每一趟在下一趟开始前释放租约。
- **别在写路径上清扫。** 清扫会写墓碑并触发 compaction；和应用写入内联跑，等于把两次写入尖峰
  叠起来、尾延迟翻倍。放到专门的维护循环里。
- **把放大量出来。** [读放大基准](../../benchmarks/bench_read_amplification.py) 和
  [预发版报告](../reports/benchmark-2024.md) 量化了多出来的键空间和墓碑对物理 IO 做了什么，
  让成本是个数字而不是一种直觉。
