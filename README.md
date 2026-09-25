# rabin_cdc — 内容定义分块（抗边界扰动切分）可复用模块

离线数据存储场景的 content-defined chunking 题库：用 GF(2) 多项式余数（Rabin 型
指纹）在字节流中滚动寻找切块点，使局部插入/删除只影响有限区域，后续块边界
自动重新对齐，从而可按内容哈希复用未改动的块。

- **Python**：3.14.7（仅用标准库：`hashlib`、`array`、`dataclasses`）
- **依赖**：无第三方依赖，`pip install` 不需要任何包；完全离线，不访问网络、
  账号、密钥、数据库、中间件、云 API、Docker 或特殊硬件。
- **平台**：Linux 原生运行。

## 准确命令

```bash
# 必要测试（含独立慢速长除法参考对拍、边界上下文逐字节断点对拍）
./.venv/bin/python -m unittest discover -s tests -v

# 固定输入演示：真实计算约 0.2 秒（远低于 90 秒预算），打印正常结果与实际触发的拒绝
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

### 边界上下文：分段处理同一字节流

```python
from rabin_cdc import Chunker

left = Chunker()
blocks = left.add(segment_a)          # 处理相邻区段 [0, cut)
ctx = left.export_context()           # 导出边界上下文（不提交任何东西）

right = Chunker.from_context(ctx)     # 在另一段流程中精确续传
blocks += right.add(segment_b)        # 处理 [cut, end)
tail = right.finish()                 # 只有显式 finish 才提交尾块
```

`export_context()` 把切分边界状态完整快照为不可变的 `BoundaryContext`
（frozen dataclass，字段均为 `int` / `str` / `bytes` 值类型）：

| 字段 | 含义 |
|---|---|
| `format_version` | 上下文格式版本（当前 `CONTEXT_FORMAT_VERSION = 1`） |
| `parameters` | 参数指纹 `PARAMETERS_FINGERPRINT`：模多项式、窗口、块区间、切点掩码、指纹位宽、流上限的规范串之 SHA-256 前 16 位十六进制 |
| `offset` | 累计绝对偏移（已消费字节数） |
| `window` | 当前 32 字节滚动窗口（最旧字节在前），`len == min(offset, 32)` |
| `fingerprint` | 窗口的 16 位余数 |
| `pending` | 未闭合块的**完整载荷**（`0 <= len < 256`），不是只有最近 32 字节——否则未输出块的 SHA-256 输入就丢了 |

保证与校验：

- **结果等价**：在任意字节边界切开、导出、换新 chunker 续传剩余区段并
  `finish()`，得到的块区间、SHA-256 与切块原因与连续切分**完全相同**
  （测试对每个字节边界逐一核对）。
- **窗口不重置**：滚动窗口与指纹随上下文整体携带，跨段连续滑动。
- **恢复前整体校验**：`from_context` 先校验格式版本、参数指纹、
  `0 <= offset <= 1 MiB`、`len(window) == min(offset, 32)`、
  `len(pending) <= offset`、`len(pending) < 256`，并用**直接多项式长除**
  （`stream_remainder`，不调用滚动更新表）从窗口字节重算余数与
  `fingerprint` 比对；任一不一致抛 `InvalidContextError`，不建造任何状态。
- **隔离性**：导出时窗口与未闭合块载荷都被复制为 `bytes`；调用方之后修改
  任何手中对象（包括用来手工构造上下文的 `bytearray`）都不影响活状态；
  原 chunker 可继续喂入甚至 `finish()`，上下文仍可重复用于多次恢复。
- **不提交尾块**：导出只是快照；尾块仍只由显式 `finish()` 提交。
- `finish()` 之后再 `export_context()` 抛 `StreamFinishedError`。

`Block` 为不可变 dataclass（`frozen=True`）：

| 字段 | 含义 |
|---|---|
| `start` / `end` | 相对整条流的绝对半开区间 `[start, end)` |
| `size` | `end - start` |
| `sha256` | 该块载荷的小写十六进制 SHA-256 |
| `reason` | `"content"`（低 6 位为 0）/ `"max"`（256 强切）/ `"tail"`（非空尾块） |

常量：`MIN_BLOCK_BYTES=64`、`MAX_BLOCK_BYTES=256`、`MAX_TOTAL_BYTES=1<<20`、
`WINDOW_BYTES=32`、`CUT_MASK=0x3F`、`POLY=0x11021`、
`CONTEXT_FORMAT_VERSION=1`、`PARAMETERS_FINGERPRINT`（随参数固定）。
`stream_remainder(data)` 暴露直接长除余数（恢复校验与测试参考用）。

## 错误语义（可定位，不静默纠正）

异常都派生自 `rabin_cdc.CDCError`：

- `InvalidBatchTypeError`（同时是 `TypeError`）：`chunks` 不可迭代、直接传入
  单个 `bytes`、某批次不是 bytes-like、`memoryview` 非一维 `B` 格式。消息带
  批次下标（如 `batch 1: ...`）。`bool` 不会被当作整数批次接受；`float`
  （含 NaN/±Infinity）一律拒绝而非隐式转换。
- `InputTooLargeError`（同时是 `ValueError`）：累计输入会超过 1 MiB。
- `StreamFinishedError`：`finish()` 后再 `add()`/`finish()`/`export_context()`。
- `InvalidContextError`（同时是 `ValueError`）：边界上下文恢复校验失败——
  格式版本不符、参数指纹不符、偏移越界、窗口长度与偏移不一致、指纹与窗口
  直接长除余数不符、未闭合块长度越界（> 偏移或 ≥ 256）等。
- **有状态操作原子性**：类型与上限检查在消费任何字节之前完成；被拒批次不会
  部分进入内部状态，随后可继续喂入合法批次，结果与从未提交坏批次一致。
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
  不重置指纹状态。
- 全零数据的 32 字节零窗口余数为 0，因此零流恰好在每个 64 字节处按
  `content` 切块（这是数学定义的直接结果，测试将其固定为向量）。
- `split_stream` 要求“批次的可迭代对象”而非单个 bytes，以区分“一批”与
  “一个流”，避免把 `bytes` 误当字符序列逐字节迭代。

## 已知限制 / 非目标

- 只实现本题契约的有限数学子集：单一固定模多项式、32 字节窗口、6 位切点、
  [64,256] 区间、1 MiB 上限；**不宣称**兼容任何行业分块标准/指纹规范（如
  rsync、FastCDC 等），也不提供其参数集。
- 单流上限 1 MiB 是接口边界；超限直接报错，不做磁盘溢出。
- `BoundaryContext` 是进程内不可变对象（字段为 `int`/`str`/`bytes`，可
  直接 pickle/放入 `dataclasses.asdict` 自行序列化）；本库**不提供**落盘
  格式、文件同步或编辑后增量更新——上下文只对“同一字节流的相邻区段”
  有效，源字节流被改动后旧上下文即失效。
- 非线程安全：一个 `Chunker` 不应被多线程并发 `add`。
- 内容寻址的“块存储后端/去重库”不在本题范围内，demo 只展示复用比例。

## 仓库布局

```
rabin_cdc/
  __init__.py        公开接口导出
  chunker.py         split_stream / Chunker / Block / BoundaryContext（滚动切分核心）
  _fingerprint.py    GF(2) 余数、三张滚动更新表、直接长除 stream_remainder
  _errors.py         异常类型
tests/
  _slow_reference.py 独立慢速参考：逐位长除、每位置重算窗口（不导入被测核心）
  _data.py           确定性 LCG 样例数据（无外部附件）
  test_fingerprint.py 指纹/表/滚动与长除一致性
  test_chunker.py    契约、零数据、窗口前缀、任意分批等价、插入一字节恢复、
                     错误语义、原子性、上限、fuzz 对拍
  test_context.py    边界上下文：逐字节断点两段对拍、刚强切后/差一字节/空尾段、
                     隔离性、整体校验拒绝、直接长除参考
demo.py              固定输入演示（复用块比例 + 分段续传 + 真实拒绝边界）
```

独立参考 `tests/_slow_reference.py` **不导入** `rabin_cdc`，期望值全部由原始
数学定义重新推出；“同一数据多种喂法结果一致”的往返检查仅作为补充测试。
