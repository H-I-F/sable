[English](atomic-flush.md) | [简体中文](atomic-flush.zh-CN.md)

# 原子落盘：temp → fsync → rename → fsync(parent)

`sable` 里每一次持久文件替换都要走的四步序列，每一步为什么存在、跳过它会坏在哪儿，以及同一段
代码在无法兑现它的平台上怎么表现。

参考实现：`src/sable/fsutil.py`。

---

## 1. 序列

```python
tmp_path = target.with_name(target.name + ".tmp-sable")
fd = os.open(str(tmp_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
with os.fdopen(fd, "wb") as handle:
    handle.write(payload)
    handle.flush()          # Python buffer  -> OS page cache
    os.fsync(handle.fileno())   # page cache  -> stable storage
os.replace(tmp_path, target)    # 原子替换
fsync_directory(target.parent)  # 目录项 -> 稳定存储
```

被持久化的其实是四样不同的东西，每一步各自管一样：

| 步骤 | 持久化了什么 | 跳过它的代价 |
| --- | --- | --- |
| 临时文件，与目标是同目录兄弟 | — | 临时文件放在别处（例如 `/tmp`）时，跨设备 `rename` 会以 `EXDEV` 失败。同目录的兄弟文件，是保证 rename 只是目录项交换而不是拷贝的唯一办法。 |
| `flush` | 进程 buffer → 内核 | 在这台机器上什么也不会发生；进程一死就全丢，因为字节从没离开用户空间。 |
| `fsync(file)` | 文件**内容** | 一次崩溃会在最终路径上留下一个零长度或只填了一半的文件。内容没了，名字还在。 |
| `os.replace` | — | 原地写意味着 reader 能看到写了一半的文件。`replace` 是原子的，reader 看到的要么旧要么新，绝不会是混合物。 |
| `fsync(parent)` | **目录项** | 经典的静默失败：数据完好无损，rename 却没落盘，于是重启后文件不见了、或者还是旧内容。 |

### 用 `os.replace` 而不是 `os.rename`

`os.rename` 在 Windows 上遇到目标已存在会失败。`os.replace` 在 POSIX 和 Windows 上都原子地覆盖。
这里每次发布都是替换（manifest、SSTable、CURRENT），所以 `replace` 在两个平台上都是正确的原语，
还省掉一个按 OS 分支。

### 为什么整份文件要在发布之前组装好

`SSTableWriter.finish()` 在内存里造出完整镜像——头部、数据块、稀疏索引、Bloom 块、footer——
然后一次性交给 `atomic_write_bytes`。最终路径上没有任何增量追加。由此得到的结论很强，值得
说明白：

> 一张 SSTable 要么完整，要么不存在。没有第三种状态，所以 reader 永远不需要为「文件存在但被
> 截断了」写处理逻辑。

这正是 `tests/test_crash.py::test_crash_never_publishes_a_partial_sstable` 能下绝对断言、而不必
下概率断言的原因。

---

## 2. 跳过目录 fsync 会得到什么故障

几乎所有手写实现都会漏掉这一步，而它的缺席又最难发现，因为它只在掉电时失败（杀进程的测试
永远看不出来）。

一次 rename 是两次逻辑写：文件的数据块，以及目录里 `000004.sst` → inode 的映射。`fsync(file)`
管第一次。没有 `fsync(dir)`，第二次在机器掉电时可能还留在 page cache 里。重启之后：

- 目录项可能还指向**旧的** inode（你的新 SSTable 消失了，而引擎的 manifest 引用着一个内容
  已经过时的文件），或者
- 目录项整个不见了，哪怕那些数据块好端端躺在磁盘某处、footer 也完全有效。

两者都是「数据持久了，名字没有」。在 `sable` 里 manifest 自己也是走同一条路径发布的，所以丢掉
一个目录项，意味着要么 manifest 描述了不存在的文件，要么文件存在却没人引用——而恢复代码没有
办法分辨到底发生了哪一种。

同样的道理也适用于 `unlink`。删除一张退役的 SSTable 同样是一次目录变更，所以
`Engine._discard_retired_files` 会把删除批量化，然后整批只调一次 `_seal_home_directory()`，
而不是每个文件调一次。

---

## 3. 平台行为与优雅降级

目录 `fsync` 并非到处都有。`src/sable/fsutil.py` 在两处处理它，而重要的设计决策是*怎么降级*。

```python
flags = getattr(os, "O_DIRECTORY", os.O_RDONLY)
try:
    fd = os.open(os.fspath(path), flags)
except OSError:
    return
try:
    os.fsync(fd)
except OSError:
    return
finally:
    os.close(fd)
```

### Windows / NTFS 上到底发生什么

`os.open()` 打开目录在那儿不是受支持的操作。底层 CPython 调的是 `CreateFile`，而打开目录句柄
需要 `FILE_FLAG_BACKUP_SEMANTICS`，`os.open` 这条路径不会传。调用以 `ERROR_ACCESS_DENIED`
失败，CPython 把它呈现为 `OSError`——原生构建上是 `PermissionError`、errno 13（`EACCES`）。
通过 POSIX 模拟层（MSYS2、Cygwin、某些 WSL 挂载）时，同一次尝试会报 errno 22（`EINVAL`，
「无效参数」）。拒绝目录句柄的网络与虚拟文件系统、以及卷驱动没在目录 fd 上实现 `fsync` 的
容器运行时，也会给 `EINVAL`。

**实用规则：捕获 `OSError`，绝不针对某一个 errno。** 特判 `errno.EINVAL` 的代码会在这个平台上
正常工作、却在另一个平台上悄悄关掉持久性，或者反过来。`sable` 用
`getattr(os, "O_DIRECTORY", os.O_RDONLY)` 也是同一个理由——Windows 上没定义 `O_DIRECTORY`，
所以这个标志是探测出来的，不是假定的。

### 降级是写明的，不是藏起来的

静默 return，等于把一条硬保证变成一种期望。正确的工程反应不是假装，而是在读者会看到的地方把
适用边界写清楚。`README.md` 和 `README.zh-CN.md` 都记录了：在拿不到目录句柄的地方，目录项的
持久性降级为该平台自己给出的保证；完整的四步不变量成立于 POSIX 文件系统。

具体在 Windows 上意味着：

- **文件内容仍然持久。** 普通文件句柄上的 `fsync` 是有效的。
- **原子替换仍然发生。** `os.replace` 在 NTFS 上是原子的。
- **目录项本身没有被显式刷盘。** NTFS 会给元数据记日志，实践中 rename 能活过掉电，但那是
  文件系统的保证，不是这段代码给的。恢复仍然正确，因为 manifest 会被校验（magic + CRC），
  任何它点名却打不开的文件都会被当成硬错误，而不是被悄悄跳过。

### 对测试的影响

一个断言「没有半截文件活下来」的崩溃测试在每个平台上都成立，因为它依赖的是 `replace` 而不是
目录 fsync。一个断言「rename 已经落盘」的测试，断言的会是平台未必提供的东西。只断言不变量在
被测平台上真正给得出的那部分——这就是 `tests/test_crash.py` 检查 footer 和 sequence 连续性、
而不检查目录状态的原因。

---

## 4. 失败清理

```python
try:
    ...
    os.replace(tmp_path, target)
except OSError:
    tmp_path.unlink(missing_ok=True)
    raise
```

rename 之前任何一步失败，同目录的临时文件都会被删掉，`OSError` 继续往上抛。不存在哪条路径会让
一次失败的发布在最终名字上留下半成品，也不存在哪条会留下一个 `.tmp-sable`。
`tests/test_sstable.py` 靠注入一次 I/O 失败、事后检查目录来断言这一点。

注意顺序：`except` 里先 unlink 再重新抛出，而目录 fsync 发生在 `try` 块*之后*。失败的发布没有
什么需要封存，因此绝不能调 `fsync_directory`。

---

## 5. 检查清单

- [ ] 临时文件是目标的**同目录兄弟**，放在同一个目录里。
- [ ] rename 之前，在文件描述符上先 `flush` 再 `fsync`。
- [ ] 用 `os.replace`，不用 `os.rename`，也不原地写。
- [ ] 每一次必须活下来的 create / rename / unlink 之后，`fsync` 父目录。
- [ ] 删除批量化，然后只封存目录一次。
- [ ] 目录 fsync 周围捕获 `OSError`，绝不针对某个 errno；`O_DIRECTORY` 要探测。
- [ ] 把降级后的保证写明，而不是暗示完整的那条。
- [ ] 失败时删掉临时文件，再重新抛出。
- [ ] 绝不让 reader 看到第三种状态：完整或不存在，没有别的。

---

## 6. 接着读哪儿

| 关注点 | 源码 |
| --- | --- |
| 四步原语 | `src/sable/fsutil.py` |
| 表的整镜像发布 | `src/sable/sstable.py` 里的 `SSTableWriter.finish` |
| manifest + CURRENT 处理 | `src/sable/engine.py` 里的 `Engine._publish_manifest` |
| 注入 I/O 失败的测试 | `tests/test_sstable.py` |
| 十个 kill 点上的崩溃断言 | `tests/test_crash.py` |
| 日志层的持久性 | [WAL 与恢复](wal-and-recovery.zh-CN.md) |
