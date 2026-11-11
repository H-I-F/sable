[English](./CONTRIBUTING.md) | 简体中文

# 给 sable 提代码

`sable` 是 LSM-tree 存储引擎的参考实现：纯 Python、只用标准库、零运行时依赖。这条约束本身就是
重点——代码被读的次数不比被跑的少——所以一个补丁的门槛是「这能让人看懂东西到底怎么运转吗」，
而不是「这加了个功能吗」。

这篇讲怎么让改动合进去。写得刻意地具体：这里每条命令都是 CI 跑的，这里每条规则 reviewer 都会
拿来卡你。

## 目录

1. [搭环境](#1-搭环境)
2. [四道门禁](#2-四道门禁)
3. [代码约定](#3-代码约定)
4. [测试](#4-测试)
5. [提交信息](#5-提交信息)
6. [拉取请求](#6-拉取请求)

## 1. 搭环境

Python 3.10 或更新。运行时不需要别的东西。

```bash
git clone https://github.com/H-I-F/sable.git
cd sable
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
python -m pip install -U pip
python -m pip install -e ".[dev]"
```

`.[dev]` 以可编辑模式装包，外加固定版本的工具链：`ruff`、`mypy`、`pytest`、`pytest-cov`。用它，
别分开装——CI 解析的是同一组 extras，所以你机器上能过的版本在 CI 里也能过。

写代码之前先确认环境：

```bash
ruff --version                     # 应与 .[dev] 一致
mypy --version                     # 应与 .[dev] 一致
pytest -q                          # 动手之前套件本来就是绿的
```

干净 checkout 上套件不绿，那是个值得先报的 bug，别等它变成赖到你头上的 bug。

`.[test]` 给只想跑套件的人：装 `pytest` 和 `pytest-cov`，不带 linter。CI 的 test job 用的就是它，
所以测试本身绝不能 import `ruff` 或 `mypy`。

## 2. 四道门禁

每次推到 `main`、每个 PR，CI 都跑四个 job。和本地能跑的命令一模一样，顺序也一样，四个都得过。

| 门禁 | 命令 | 守住什么 |
| --- | --- | --- |
| Lint | `ruff check src tests examples benchmarks` | 格式、import 顺序、未使用的名字，以及能抓出生产代码里 `assert` 的安全规则 |
| Types | `mypy src` | 公开面：每个函数都有标注，而且标注站得住 |
| Tests | `pytest -q --cov=sable --cov-fail-under=85 tests/` | 行为，外加覆盖率下限 |
| Examples | `python examples/basic.py && python examples/crash_demo.py` | 文档里的代码还能跑 |

这两处是刻意的：

- **Lint 覆盖 `examples` 和 `benchmarks`，不只是 `src`。** 那两棵树就是文档，不 lint 的文档比它
  描述的代码烂得更快。本地跑 `ruff check .` 等价，没问题。
- **`mypy` 只覆盖 `src`。** 测试靠跑来检验正确性，基准靠跑；两边都要类型，付出比收获大。当然
  愿意补标注也欢迎。

覆盖率下限是 **85%**，由 `--cov-fail-under` 强制。套件目前是 **94.1%** 语句覆盖，所以一个补丁
有空间让这个数字动一动而不被卡住——但空间不大。加了一条没人走到过的分支，会被要求么补测试、
要么写注释说明为什么它不可达并排除掉。

按 CI 的方式一次跑完：

```bash
ruff check src tests examples benchmarks
mypy src
pytest -q --cov=sable --cov-fail-under=85 tests/
python examples/basic.py && python examples/crash_demo.py
```

## 3. 代码约定

下面每条都有工具兜着，除非另外标注。

- **行长 100，目标 Python 3.10。** 都来自 `pyproject.toml`；别手工绕着它们重排，跑
  `ruff check --fix`。
- **每个函数都有标注。** `disallow_untyped_defs` 是开着的。公开签名里出现 `Any`，需要在 docstring
  里给个理由。
- **不引第三方包。** 引擎只 import 标准库。一个补丁如果需要依赖，得解释清楚这个依赖凭什么比它
  损失的可读性更值钱。
- **docstring 写为什么，不写是什么。** `"""冻结活跃 memtable 并发布为新的 L0 表。"""` 值它那一行；
  把签名复述一遍的 docstring 不值。不显然的理由放 docstring，长篇版本放 `docs/decisions/` 当 ADR。
- **不留 `TODO`、`FIXME` 或占位代码。** 没做完就不进去。做完了但粗糙，写进 PR 描述里让 reviewer
  掂量。
- **错误来自 `sable.errors` 的层级。** 调用方理应能处理的用 `SableError`；磁盘上的损坏用
  `CorruptionError` 及其子类。绝不抛裸 `Exception`，也绝不悄悄吞掉一个。
- **持久性没得商量。** 写路径上的任何东西都必须守住 ADR-004 写明的那条不变量：已确认的写入
  扛得住崩溃。拿它换吞吐的补丁得放在一个显式选项后面，而那个选项需要一篇 ADR。

## 4. 测试

测试住在 `tests/`，与它覆盖的模块对应，也是 reviewer 第一个读的部分。

**什么样的测试值得加：**

- 改前代码上会挂、改后会过。两边都过，那是文档，该放 `examples/`。
- 断言的是*不变量*，不是当前输出。`assert engine.get(k) == v` 是一个事实；
  `assert engine.level_files[0] == []`（flush 失败之后）是一条性质。
- 经得起重跑。带种子的一律固定种子；测时间的一律断言上界，不定死一个值。

**崩溃矩阵就是这个项目存在的理由。** 碰了 WAL、flush 路径或 compaction，就加一个在你改的那个点
上失败的用例。`tests/` 里现成有注入助手，能把写入拦在半帧、弄坏块 CRC、耗尽磁盘、泄漏文件
描述符——用它们，别另造一套（范式见 `tests/test_chaos_enospc.py`：按文件系统真实失败的方式去
patch 那个 syscall，而不是按代码调用它的方式）。

**覆盖率是下限，不是目标。** 一个从没在并发下被正确调用过的函数，100% 行覆盖，还不如 90% 加一个
把 compaction 交错进来的混沌测试。

## 5. 提交信息

用 [Conventional Commits](https://www.conventionalcommits.org/)，type 表意图，scope 点名模块：

```
feat(sstable): add Bloom filter to the table footer
fix(engine): keep the WAL position stable across a failed flush
perf(scan): reuse the merged iterator between adjacent blocks
test(chaos): inject ENOSPC at fsync rather than at write
docs(recipes): document TTL eviction on top of tombstones
bench(read-amp): measure physical block reads per logical read
build(meta): add a Makefile for the local gates
ci(workflow): run the examples as part of the test job
chore(progress): tick PR 22
```

| 类型 | 用在哪 |
| --- | --- |
| `feat` | 公开 API 原本没有的能力 |
| `fix` | 行为错了，包括崩溃和损坏 |
| `perf` | 唯一理由是一个测出来的数字——把数字引上 |
| `test` | 只动测试，不动生产代码 |
| `docs` | 散文、ADR、配方、README |
| `bench` | 测量套件和它们的报告 |
| `build` | 不是 CI 的工具：Makefile、打包 |
| `ci` | workflow |
| `chore` | 对产物没影响的杂务 |

reviewer 会检查的规则：

- **标题用祈使句、小写、结尾不加句号、≤ 72 字符。** Git 自己的输出从标题开始，所以 `add`
  读起来是「这个提交添加了」。
- **正文写为什么，不写是什么。** diff 已经说了是什么。如果正文塞得进标题，这个改动就小到不需要
  正文。
- **一个提交一件事。** 一个既修 bug 又重排文件的提交没法干净地 revert，而这是唯一要紧的
  提交检验标准。

## 6. 拉取请求

### 开之前

1. 四道门禁按 §2 的顺序在本地全过。
2. 分支 rebase 到当前 `main`，不是把 `main` merge 进来。
3. 每个提交都自洽：在那一个提交上，树能构建、测试是绿的，而不只是在分支尖端。

### 分支名

`<type>/<short-topic>` —— `feat/bloom-filter`、`fix/wal-torn-tail`、`docs/recipes-ttl`、
`bench/read-amplification`。小写、连字符、名字里别带 PR 编号：PR 重编号时数字会变，名字不会。

### reviewer 看什么

| 问题 | 为什么它能决定评审结果 |
| --- | --- |
| 改动守住了持久性不变量吗？ | 别的都能谈，这条不能 |
| 有没有一个不加这个补丁就会挂的测试？ | 没有失败测试的 bug 修复只是猜测 |
| 它加依赖了吗？ | 只用标准库是一条特性 |
| docstring 在解释*为什么*吗？ | 代码就是文档 |
| 提交能独立成立吗？ | 历史被读的次数远多于被写的次数 |

### 怎么合并

维护者用 **rebase** 合并，让 `main` 保持线性，并且落进去的每个提交都是被审过的那一个。两个后果：

- **PR 开着的时候尽管 force-push 你的分支。** 反正合并时历史会被重写，事先收拾干净没有代价，
  而把四条「wip」「fix typo」「actually fix typo」落进历史是有代价的。
- **别把 `main` merge 进你的分支。** 要 rebase 上去。PR 里的一个 merge commit，在 PR 被 rebase
  时会作为噪音活进 `main`。

合并前 CI 必须绿，没有例外。一个「和本改动无关」的红构建仍然是红构建；如果 `main` 本身坏了，
先用自己的 PR 把它修好，因为 reviewer 分不清哪些是你弄坏的、哪些是本来就坏的。

### 改报 bug

开 issue 带上版本、最小的复现脚本和你的预期。凡是丢数据或损坏数据的，第一行就写明——这类
issue 会插队。一个带着失败后留下的 WAL 或 SSTable 的复现，抵得上十个只用文字描述的，检查器
正是为这个准备的：

```bash
python -m sable.inspector path/to/wal-000001.log      # 逐帧 WAL 审计
python -m sable.sstable_inspector path/to/000001.sst  # 表头、索引、Bloom 几何
python -m sable.topology path/to/db                   # 哪个文件装了什么
```

每一个都读引擎写出来的文件、打印看到的东西；都不需要引擎处于打开状态，这正是它们能拿来对付
失败运行残骸的原因。
