# rabin_cdc — 内容定义分块（抗边界扰动切分）可复用模块

离线数据存储场景的 content-defined chunking 题库：用 GF(2) 多项式余数（Rabin 型
指纹）在字节流中滚动寻找切块点，使局部插入/删除只影响有限区域，后续块边界
自动重新对齐，从而可按内容哈希复用未改动的块。

本增量在原有连续切分之上增加**可拼接的边界上下文**：同一条字节流的相邻区段
可以分别在独立会话中处理（中途导出上下文、稍后从上下文继续），最终块列表与
一次性连续切分**完全相同**（绝对区间、SHA-256、切块原因）。

- **Python**：3.14.7（仅用标准库：`hashlib`、`array`、`dataclasses`、`struct`）
- **依赖**：无第三方依赖，`pip install` 不需要任何包；完全离线，不访问网络、
  账号、密钥、数据库、中间件、云 API、Docker 或特殊硬件。
- **平台**：Linux 原生运行。

## 准确命令

```bash
# 必要测试（含独立慢速长除法参考对拍与每字节边界两切分核对）
./.venv/bin/python -m unittest discover -s tests -v

# 固定输入演示：真实计算约 1 秒（远低于 90 秒预算），打印复用比例、
# 边界上下文拼接（每字节边界、强切后、差一字节、空尾段）与实际触发的拒绝
./.venv/bin/python demo.py
```

## 算法契约（固定）

| 项 | 值 |
|---|---|
| 指纹 | 字节流多项式在 GF(2) 上除以 P(x) 的 16 位余数 |
| 模多项式 | P(x) = x¹⁶ + x¹² + x⁵ + 1（位向量 `0x11021`） |
| 窗口 | 32 字节滚动窗口，**跨块不重置**，沿整个字节流滑动 |
| 最小块 | 64 字节（尾块除外） |
| 最大块 | 256 字节，到达即强制切块 |
| 内容切点 | 块长达到 64 后，窗口余数**低 6 位为 0**（`fp & 0x3F == 0`）的首个位置 |
| 切点优先级 | 长度 256 的强制切（`max`）优先于同位置的内容切 |
| 输入上限 | 单条流累计最多 1 MiB（`MAX_TOTAL_BYTES = 1048576`） |

字节位序约定：字节按到达顺序构成多项式，首字节占最高字节槽，字节内 bit 7
最先入流（bit k 是 xᵏ 的系数）。窗口未满（流的前 1..31 字节）时，指纹为当前
前缀自身的余数；满 32 字节后为最近 32 字节窗口的余数。

### 滚动更新（不为每位置重算窗口）

每来一个字节，余数用三张导入时一次性建好的 256 项表常数时间更新：

```
f' = SHIFT[hi(f)] XOR BYTE[lo(f)] XOR REMOVE[b_out] XOR b_in
```

- `SHIFT[hi] = hi·x¹⁶ mod P`，`BYTE[lo] = lo·x⁸ mod P`，合起来实现 `f·x⁸ mod P`；
- `REMOVE[b] = b·x²⁵⁶ mod P`，抵消离开 32 字节窗口的旧字节（窗口未填满时无此项）；
- 表本身用逐位 GF(2) 长除和平方累乘在本库内构建，不调用任何第三方指纹实现。

## 公开接口与数据结构

```python
from rabin_cdc import split_stream, Chunker, Block

blocks: list[Block] = split_stream(chunks)
```

- `split_stream(chunks)`：接收**字节批次的可迭代对象**（`bytes` /
  `bytearray` / `memoryview`，允许空批次），返回覆盖 `[0, total)` 的
  `Block` 列表；空流返回 `[]`。同样的字节无论怎样分批（1 字节一批、整流一
  批、任意跨切点分批、夹杂空批次）结果完全相同。
- `Chunker()`：等价的有状态 API。`add(batch) -> list[Block]` 喂入一批并返回
  本批完成的块；`finish() -> Block | None` 结束流，仅当存在非空尾块时返回
  一个 `reason="tail"` 的块。`finish()` 只能成功调用一次。

`Block` 为不可变 dataclass（`frozen=True`）：

| 字段 | 含义 |
|---|---|
| `start` / `end` | 相对整条流的绝对半开区间 `[start, end)` |
| `size` | `end - start` |
| `sha256` | 该块载荷的小写十六进制 SHA-256 |
| `reason` | `"content"`（低 6 位为 0）/ `"max"`（256 强切）/ `"tail"`（非空尾块） |

常量：`MIN_BLOCK_BYTES=64`、`MAX_BLOCK_BYTES=256`、`MAX_TOTAL_BYTES=1<<20`、
`WINDOW_BYTES=32`、`CUT_MASK=0x3F`、`POLY=0x11021`。

## 边界上下文：分段处理同一条流（新增）

相邻区段分别处理，最终得到与连续切分完全相同的块。分段结束**不重置 Rabin
窗口**，也**不提交尾块**——尾块仍只由明确的 `finish()` 输出。

```python
from rabin_cdc import (
    Chunker, export_context, restore_context,
    serialize_context, deserialize_context,
)

# 第一段（一个独立会话；可以是另一进程/机器/稍后时刻）
first = Chunker()
blocks = first.add(data[:cut])          # 已闭合的块照常产出
snapshot = export_context(first)        # 不可变、与活状态完全脱离
blob = serialize_context(snapshot)      # 可选：自描述字节 blob（含整体 SHA-256）

# 第二段：从上下文继续输入
second = restore_context(deserialize_context(blob))
blocks += second.add(data[cut:])        # 窗口/指纹从未闭合块原样续上
tail = second.finish()                  # 只有这里才提交 tail
```

`export_context(chunker) -> ContextSnapshot` 保存：

| 字段 | 含义 |
|---|---|
| `version` | 快照格式版本（`CONTEXT_FORMAT_VERSION=1`） |
| `parameters` / `param_fingerprint` | 固定算法参数的只读映射及其 SHA-256 指纹 |
| `window` / `window_len` | **完整**滚动窗口（最旧字节在前）与其长度 `min(32, total)` |
| `fingerprint` | 当前窗口的 16 位余数 |
| `open_block` / `block_start` | **全部**尚未输出的块内容及其绝对起点（不是最近 32 字节） |
| `total` | 累计绝对偏移（已接受字节数） |
| `finished` | 流是否已 finish |
| `open_sha256` | 未闭合块内容的 SHA-256（冗余摘要，防载荷被改） |

关键语义：

- **恢复前整体校验**：格式版本、参数指纹、字段类型/范围必须匹配；窗口长度、
  未闭合块长度与累计偏移必须相互一致（`len(open_block) == total - block_start`、
  `window_len == min(total, 32)`、窗口与未闭合块必须互为后缀）；并用**直接
  多项式长除**（不查滚动更新表）重算窗口余数，与 `fingerprint` 核对。任何一项
  不符抛 `ContextError`，校验在构造任何 chunker 状态之前完成（全有或全无）。
- **窗口跨段不重置**：快照始终带完整 32 字节窗口与当前指纹；尚未输出块的
  SHA 输入（`open_block`，可能远长于 32 字节）完整保留，不会被窗口覆盖丢失。
- **不可变且脱离活状态**：`ContextSnapshot` 为 `frozen` dataclass，字节字段为
  `bytes`、参数映射只读；导出做拷贝，调用方修改导出对象（或继续喂活 chunker）
  互不影响；恢复时再次拷贝。
- `serialize_context` 产出 `RCDC1` 开头的大端自描述 blob，末尾附整体
  SHA-256；`deserialize_context` 先验完整性，恢复时仍走全部语义校验。

## 错误语义（可定位，不静默纠正）

异常都派生自 `rabin_cdc.CDCError`：

- `InvalidBatchTypeError`（同时是 `TypeError`）：`chunks` 不可迭代、直接传入
  单个 `bytes`、某批次不是 bytes-like、`memoryview` 非一维 `B` 格式。消息带
  批次下标（如 `batch 1: ...`）。`bool` 不会被当作整数批次接受；`float`
  （含 NaN/±Infinity）一律拒绝而非隐式转换。
- `InputTooLargeError`（同时是 `ValueError`）：累计输入会超过 1 MiB。
- `StreamFinishedError`：`finish()` 后再 `add()`/`finish()`；恢复一个
  `finished=True` 的快照后再喂入也抛此错。
- `ContextError`（同时是 `ValueError`）：边界上下文格式版本/参数指纹不被本
  构建支持、字段类型或范围非法、窗口长度/未闭合块长度/累计偏移互相矛盾、
  窗口与未闭合块后缀不一致、携带指纹与窗口直接长除结果不符、未闭合块摘要
  不符，或序列化 blob 截断/魔数错误/整体 SHA-256 校验失败。
- **有状态操作原子性**：类型与上限检查在消费任何字节之前完成；被拒批次不会
  部分进入内部状态，随后可继续喂入合法批次，结果与从未提交坏批次一致。
  上下文恢复同样是全有或全无：拒绝时不存在“半构造”的 chunker。
- 本算法核心不存在搜索/求解过程，因此没有“预算耗尽”类错误；测试中的确定性
  小范围枚举若找不到构造值会显式失败，与“证明无解”不做混淆。

## 浮点 / 误差口径

- 核心与所有判定（切点、边界、SHA-256）全部是整数与精确 GF(2) 运算，**没有
  浮点、没有容差**；测试均为精确相等断言。
- 仅 `demo.py` 打印的“复用比例”由精确整数分数（块数/块数、字节数/字节数）
  经 `:.4%` 四舍五入到小数点后 4 位显示，仅用于展示，不参与任何正确性判定。

## 设计取舍

- 切点概率固定为 1/64（低 6 位为 0），平均块长约 128 字节，夹在 [64, 256]
  之间；参数按题面写死，不暴露调参接口（`ConfigurationError` 预留给将来的
  参数化构造，当前无入口会触发）。
- 用环形 32 字节缓冲 + 三张 256 项 `array('H')` 表做滚动；纯 Python 每字节
  数次查表/异或，本机处理 1 MiB 约 0.3 秒。
- 窗口跨块连续：这是插入后边界能快速重新对齐的关键；切块只决定输出分组，
  不重置指纹状态。边界上下文沿用同一不变量：快照里的窗口永远是
  `min(total, 32)`，与未闭合块在 `total` 处相接。
- 全零数据的 32 字节零窗口余数为 0，因此零流恰好在每个 64 字节处按
  `content` 切块（这是数学定义的直接结果，测试将其固定为向量）。
- `split_stream` 要求“批次的可迭代对象”而非单个 bytes，以区分“一批”与
  “一个流”，避免把 `bytes` 误当字符序列逐字节迭代。
- 边界上下文是**内存对象/字节 blob** 的交接，不是文件同步或编辑后增量更新
  机制：不跟踪磁盘文件、不做落盘调度；`open_block` 随未闭合块线性增长
  （达到 256 字节即切块，故快照中至多 255 字节），blob 典型约 100–400 字节。

## 已知限制 / 非目标

- 只实现本题契约的有限数学子集：单一固定模多项式、32 字节窗口、6 位切点、
  [64,256] 区间、1 MiB 上限；**不宣称**兼容任何行业分块标准/指纹规范（如
  rsync、FastCDC 等），也不提供其参数集。边界上下文只在**同参数**构建间可
  恢复；参数指纹或格式版本不同一律拒绝，不做跨版本迁移/自动转换。
- 单流上限 1 MiB 是接口边界；超限直接报错，不做磁盘溢出。快照恢复后该上限
  仍按 `total` 继续累计。
- 非线程安全：一个 `Chunker` 不应被多线程并发 `add`；`ContextSnapshot`
  本身不可变，可在多线程间自由传递。
- 边界上下文提供内存对象与字节 blob 两种形态；blob 的 SHA-256 只防意外
  损坏/篡改（无密钥），不是认证加密，不提供机密性或来源认证。
- 不做文件同步、编辑后增量更新、差异补丁或块存储后端；demo 只展示复用
  比例与分段拼接等价性。

## 仓库布局

```
rabin_cdc/
  __init__.py        公开接口导出
  chunker.py         split_stream / Chunker / Block（滚动切分核心）
  _fingerprint.py    GF(2) 余数与三张滚动更新表
  _context.py        边界上下文：ContextSnapshot / export / restore /
                     serialize / deserialize（含直接长除整体校验）
  _errors.py         异常类型（含 ContextError）
tests/
  _slow_reference.py 独立慢速参考：逐位长除、每位置重算窗口（不导入被测核心）
  _data.py           确定性 LCG 样例数据（无外部附件）
  test_fingerprint.py 指纹/表/滚动与长除一致性（既有回归）
  test_chunker.py    契约、零数据、窗口前缀、任意分批等价、插入一字节恢复、
                     错误语义、原子性、上限、fuzz 对拍（既有回归）
  test_context.py    每字节边界两切分等价、强切后/差一字节/空尾段、窗口与
                     未闭合块保留、脱离活状态、blob 往返与篡改、整体校验
demo.py              固定输入演示（复用块比例 + 边界上下文拼接 + 真实拒绝）
```

独立参考 `tests/_slow_reference.py` **不导入** `rabin_cdc`，期望值全部由原始
数学定义重新推出；“同一数据多种喂法结果一致”的往返检查仅作为补充测试。
