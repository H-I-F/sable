[English](wal-and-recovery.md) | [简体中文](wal-and-recovery.zh-CN.md)

# WAL 与 torn-tail 截断恢复

怎么造一个恢复行为能被证明、而不是被寄予厚望的预写日志。抽出自 `src/sable/wal.py`、
`tests/test_wal.py` 和 `tests/test_crash.py`。

---

## 1. 日志存在的意义就一条不变量

> 成功返回的写入，在进程死后必须仍然可读。

这篇文档里其余一切，都是为这句话服务的机械。这套机械叫 append-before-apply：

1. 分配一个单调的 sequence number，
2. 把变更序列化成一帧，
3. 对帧 `write` + `flush` + `fsync`，
4. *之后*才让变更在 memtable 里可见。

第 3、4 步绝不重排。`src/sable/engine.py` 里的 `Engine._stage_mutation` 是唯一接纳变更的入口，
它握着 `_lock` 做这件事，正是为了让这个顺序不可能被观察到是反的。

由此得到的推论，才让恢复变得可处理：**日志可以丢掉任何它还没 fsync 的东西，因为未 fsync 的
东西从未被确认过。** 所以一个会「修复」尾部的恢复算法并不是在提升持久性——它是在凭空造出没人
承诺过的数据。仅仅把框架这么一转，一个难题就变成了截断。

---

## 2. 帧的结构

```
+----------+----------+---------+-----------+-----------+------+-------+
| crc32    | sequence | op      | klen      | vlen      | key  | value |
| uint32   | uint64   | uint8   | uint32    | uint32    | klen | vlen  |
+----------+----------+---------+-----------+-----------+------+-------+
   4 B        8 B       1 B       4 B         4 B
```

21 字节头，`struct.Struct("<IQBII")`，小端，无填充。`crc32` 覆盖**它自己之后的一切**——剩余
17 个头字节加上 key 和 value。

这个形状带来三条性质，也正是采用二进制帧的全部理由：

| 性质 | 为什么重要 |
| --- | --- |
| **自描述** | 从任意字节偏移都能算出下一帧的起点，不必查外部索引。扫描器就是一个 `while offset < total` 循环。 |
| **没有分隔符歧义** | 用长度前缀，不用分隔符。键里合法地可以含 `0x0A`、`0x2C` 或任何文本格式必须转义的字节。 |
| **读是有界的** | `klen`/`vlen` 让扫描器在还没消费 payload 之前就能判定「完整」还是「半截」。 |

### 为什么不用 JSON lines

按行分隔的 JSON 日志是最容易想到的第一版。它在三条互不相干的轴上都不行：

1. **成帧。** 你得要求 payload 里没有 `\n`。而键值存储里的键和值是任意字节，于是必须转义或
   base64——也就是说每次写付一次编码、每次读付一次解码，还都在最热的循环里，只为绕过成帧
   选择带来的毛病。
2. **半写检测。** `{"k": "a` 是 10 字节语法上不完整的 JSON。要判「撕裂」还是「损坏」就得解析，
   而解析失败和一个碰巧格式不对的值无法区分。长度前缀用一次整数比较、O(1) 就答了。
3. **成本。** 走通用编码器序列化一条记录，会分配中间字符串和 dict。`struct.pack` 只往一个
   buffer 里写。在本引擎这种 fsync 主导的延迟下 CPU 不是瓶颈——但上面那些成帧性质在文本格式里
   出多少钱都买不到，而一旦拿下了它们，省下的 CPU 是白送的。

重点不是「二进制更快」，而是**自描述的定长头才让恢复决策可判定**，JSON 给不了你这一点。

### 为什么 CRC32 放在最前面

因为它覆盖的是后面的字节，reader 可以一边流式读一边单趟算摘要，不必回退 seek。放在最前面还
意味着：验完第 *i* 帧的扫描器，既确切知道第 *i+1* 帧从哪儿开始，又能在给 key/value 切片分配
内存之前先把它拒掉。

---

## 3. 恢复决策表

`src/sable/wal.py` 里的 `scan_wal_bytes(data)` 是个纯函数：`bytes -> RecoveryResult`。不碰文件
句柄、不碰时钟、不碰线程。这份纯粹是刻意的，也正是恢复矩阵能被穷举测试的原因（见 §5）。

| 在偏移 `o` 处观察到 | 分类 | 动作 |
| --- | --- | --- |
| `remaining < FRAME_HEADER_SIZE` | torn tail | 停下。丢弃 `remaining` 字节。 |
| `klen > MAX_KEY_BYTES` 或 `vlen > MAX_VALUE_BYTES` | 结构性损坏 | 抛 `CorruptWALError` |
| 未知的 `op` 字节 | 结构性损坏 | 抛 `CorruptWALError` |
| `remaining < header + klen + vlen` | torn tail | 停下。丢弃 `remaining` 字节。 |
| CRC 不匹配，**不是**最后一帧 | 真正的损坏 | 抛 `ChecksumMismatchError` |
| CRC 不匹配，**是**最后一帧 | torn tail（默认） | 停下、丢弃；`strict_tail_crc=True` 下改为抛异常。 |

### 为什么非末尾的 CRC 失败必须致命

如果一帧校验失败、而它*后面还有字节*，那就说明有更晚的写入在它之后被 fsync 过。日志是只追加
的，文件系统也不会把一个已完成的 `fsync` 重排，所以「坏帧之后还有一帧有效」没法用崩溃解释
——它意味着字节落地之后被改过。位衰减、坏磁盘块、有人编辑过文件、一份在中间被截断后又被
追加的副本。

这是性质完全不同的情形，两者绝不能共用一条恢复路径：

- torn tail → 数据从未被承诺过。丢弃是**正确的**。
- 流中段损坏 → 数据*被承诺过*并且已经丢了。静默丢弃会少报数据丢失，瞎猜更糟。

所以引擎拒绝猜，直接抛。面对「我无法重构你给我的东西」，响亮地拒绝是唯一诚实的回应。

### 为什么末尾那帧得到疑罪从无

一帧的长度字段已经落盘、payload 没落盘的半写帧，和一个翻了一位的完整帧，在字节层面无法区分。
没有任何测试能把两者分开。所以默认回答的是运维上真正要紧的那个问题：*这个文件能安全重放吗？*
能——尾部之前的一切都验过了。`strict_tail_crc=True` 是给审计用的，那里你宁愿知道尾部可疑，
也不愿悄悄把它丢掉。

---

## 4. 幂等的修复

```python
result = WriteAheadLog.recover(path)          # 纯扫描，不改任何东西
if result.torn_tail:
    removed = WriteAheadLog.truncate_torn_tail(path, result.valid_bytes)
```

修复是幂等的，因为 `valid_bytes` 是*字节镜像的纯函数*：同样的字节扫两次得到同样的偏移，而
截断到一个你已经满足的偏移是一次 no-op（有 `if valid_bytes >= size: return 0` 这个保护）。

这件事比看上去更要紧。恢复跑在 `Engine.open` 里，而 `open` 既可能被调用在一个已经恢复过的库上，
也可能被调用在一个*上一次恢复过程中*又崩了的库上。如果恢复是有状态的——「修复会推进游标」
——那么恢复中途崩一次就可能导致重复应用或跳过。做成纯函数加幂等提交，恢复跑多少次都安全，
而这正是保证它跑一次也安全的唯一办法。

`truncate_torn_tail` 截断之后会 fsync。截断同样是一次元数据变更；没 fsync 的截断可能被掉电
撤销，于是在下一次 open 时把垃圾复活。

---

## 5. 把状态机设计成可测的

整份恢复策略都住在一个纯函数里，其余一切都只是围着它的 I/O。这个切分换来三种测试形态，三种
你都想要。

### 5.1 字节镜像测试（快、穷举、确定）

直接构造镜像。没有文件系统、没有子进程，亚毫秒级：

```python
def build_image(count: int) -> bytes:
    return b"".join(
        encode_frame(InternalEntry.put(b"k-%05d" % i, i, b"v-%05d" % i))
        for i in range(count)
    )

def test_recovery_returns_three_records_when_fourth_is_truncated() -> None:
    image = build_image(4)
    # 把第四帧切成两半：一次帧中途的掉电。
    cut = len(image) - 8
    result = scan_wal_bytes(image[:cut])
    assert result.frame_count == 3
    assert result.torn_tail is True
    assert result.discarded_bytes == len(image[:cut]) - result.valid_bytes
```

因为输入就是字节，你可以廉价地在*每一个*偏移切一刀。让 `cut` 扫过 `range(len(image))`、并在
每处断言不变量成立，比跑多少次崩溃都更强，而且只要几毫秒。

### 5.2 用 `monkeypatch` 做故障注入（真实 I/O，强制走到分支）

字节镜像验的是扫描器，验不了 `append` 真的写出了扫描器所期待的东西。把 writer 改成写一半：

```python
def test_power_cut_mid_frame_leaves_a_torn_tail(tmp_path, monkeypatch) -> None:
    real = WriteAheadLog.append

    def half_write(self, entry, sync=None):
        frame = encode_frame(entry)
        self._handle.write(frame[: len(frame) // 2])
        self._handle.flush()
        os.fsync(self._handle.fileno())      # 这半帧确实是持久的
        return len(frame) // 2

    monkeypatch.setattr(WriteAheadLog, "append", half_write)
    log = WriteAheadLog.open(tmp_path / "wal.log")
    log.append(InternalEntry.put(b"a", 0, b"1"))
    log.append(InternalEntry.put(b"b", 1, b"2"))
    log.close()
    monkeypatch.undo()

    result = WriteAheadLog.recover(tmp_path / "wal.log")
    assert [e.user_key for e in result.entries] == [b"a"]
    assert result.torn_tail
```

注入测试的两条规则：

- **在你真正不信任的那道缝上注入。** 把 `os.fsync` patch 成 no-op，测的是「代码有没有调
  fsync」，那是覆盖率问题。把写 patch 成半截，测的才是「恢复分类对不对」，那是正确性问题。
- **断言分类，而不只是数量。** 如果损坏被静默吞掉，`frame_count == 1` 也会因为错误的理由通过。
  分别断言 `torn_tail`、`discarded_bytes` 和 `valid_bytes`。

### 5.3 进程级 `SIGKILL`（唯一不会撒谎的测试）

单元测试和被测代码共享解释器的假设。`SIGKILL` 不会：

```python
def kill_after_acks(home, acks, options):
    process = subprocess.Popen([sys.executable, "-c", CHILD_SOURCE, str(home), ...])
    acknowledged = []
    for line in process.stdout:            # 子进程只在 put() 返回之后才打印
        acknowledged.append(line.strip().encode())
        if len(acknowledged) == acks:
            process.send_signal(signal.SIGKILL)
            break
    process.wait()
    return acknowledged
```

子进程*在 `put()` 返回之后*打印一个 id。父进程一看到第 *n* 行就杀掉它。`SIGKILL` 不可捕获，
于是没有关闭钩子、没有 `finally`、没有退出时 flush——这是一个进程能体验到的、最接近拔插头的
东西。

于是父进程的断言是精确的而不是统计的：**子进程确认过的每个 id，在重开之后都必须可读。** 让
`acks` 遍历 `range(1, 11)`，你就有了十个不同的崩溃点，覆盖「任何 flush 之前就崩了」「flush
中途崩了」「manifest 发布之后崩了」以及中间的一切。

`tests/test_crash.py` 正是这么做的，另外还断言了负面证据：没有 `.tmp-sable` 兄弟活下来、
没有 SSTable 的 footer CRC 失败、sequence 分配从重放出的最高帧之上继续。

---

## 6. 造新 WAL 的检查清单

- [ ] 定长、自描述的头；用长度前缀，不用分隔符。
- [ ] 校验和覆盖校验和字段*之后*的帧内容，这样流式处理才成立。
- [ ] 帧只追加，绝不原地重写。
- [ ] 在告诉调用方写入成功之前 `fsync`；创建段之后 `fsync` 目录。
- [ ] 恢复是字节镜像的纯函数。
- [ ] 修复幂等，且对截断做 fsync。
- [ ] torn tail（从未承诺）与流中段损坏（承诺过且已丢失）是两种错误、两种结果。
- [ ] 三层测试：字节镜像、注入半写、`SIGKILL`。
- [ ] 日志轮转只发生在它覆盖的 memtable 已被持久发布之后，绝不提前。

---

## 7. 接着读哪儿

| 关注点 | 源码 |
| --- | --- |
| 帧编解码 / 扫描 / 修复 | `src/sable/wal.py` |
| 扫描器单测、字节镜像切割 | `tests/test_wal.py` |
| `SIGKILL` 矩阵 | `tests/test_crash.py` |
| append-before-apply 的调用点 | `src/sable/engine.py` 里的 `Engine._stage_mutation` |
| SSTable 与 manifest 的原子发布 | [原子落盘](atomic-flush.zh-CN.md) |
