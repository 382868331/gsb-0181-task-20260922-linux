# rabin_cdc — 内容定义分块（抗边界扰动切分）可复用模块

离线数据存储场景的 content-defined chunking 题库：用 GF(2) 多项式余数（Rabin 型
指纹）在字节流中滚动寻找切块点，使局部插入/删除只影响有限区域，后续块边界
自动重新对齐，从而可按内容哈希复用未改动的块。

本轮迭代在原切分能力之上增加**对象中间区间替换入口** `replace_region`：对大
对象的任意半开区间做替换（含头部/尾部编辑、纯插入、纯删除），沿用原切分规则
重新扫描整个对象，并输出旧块**保留（keep）/替换（replace）**与**新增
（new）**的稳定映射。允许整对象重扫，不做局部复杂度上界，也不做持久引用计数。

- **Python**：3.14.7（仅用标准库：`hashlib`、`array`、`dataclasses`）
- **依赖**：无第三方依赖，`pip install` 不需要任何包；完全离线，不访问网络、
  账号、密钥、数据库、中间件、云 API、Docker 或特殊硬件。
- **平台**：Linux 原生运行。

## 准确命令

```bash
# 必要测试（含独立慢速长除法参考对拍）
python -m unittest discover -s tests -v

# 固定输入演示：真实计算约 0.2 秒（远低于 90 秒预算），打印正常结果、
# 首尾/跨边界/相同内容块边界以及实际触发的拒绝
python demo.py
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

## 中间替换与块映射（本轮新增）

```python
from rabin_cdc import replace_region, diff_chunks

result = replace_region(data, offset, old_length, replacement)
```

- `replace_region(data, offset, old_length, replacement)`：把对象 `data` 的
  半开中间区间 `[offset, offset+old_length)` 替换为 `replacement` 字节，
  **不修改入参**；然后用与 `split_stream` 完全相同的规则对替换后的整个对象
  重新切分，返回 `ReplacementResult`。
  - `old_length=0` 为纯插入（在 `offset` 前插入；`offset=0` 即头部插入，
    `offset=len(data)` 即尾部追加）；`replacement=b""` 为纯删除；二者同时
    为空即空操作（切分逐块不变）。
  - `data` / `replacement` 接受 `bytes` / `bytearray` / 一维 `B` 格式
    `memoryview`；`offset` / `old_length` 必须是真实 `int`（拒绝 `bool`、
    `float`、`str` 等），满足 `0 <= offset <= len(data)`、
    `old_length >= 0`、`offset + old_length <= len(data)`。
  - 替换后对象仍受同一个 1 MiB 上限约束，超限在做任何切分之前拒绝。
- `ReplacementResult`（不可变 dataclass）字段：

| 字段 | 含义 |
|---|---|
| `old_blocks` / `new_blocks` | 替换前 / 后对象的 `Block` 列表；新块偏移相对**新对象** |
| `mappings` | `BlockMapping` 行列表，每个旧块与每个新块恰好出现一次 |
| `edit_start` / `edit_end` | 替换区在**新对象**中的半开区间（`edit_end = offset + len(replacement)`） |
| `new_data` | 替换后的完整对象字节 |

便捷属性：`kept_blocks`（保留的旧块，旧布局序）、`replaced_blocks`（消失的
旧块）、`added_blocks`（没有旧同内容块的新块）。

- `BlockMapping`（不可变 dataclass）字段：`status`（`"keep"` /
  `"replace"` / `"new"`，常量 `KEEP`/`REPLACE`/`NEW`）、`old_index`、
  `new_index`、`sha256`、`size`。
  - **keep**：两个下标都有，旧块在新布局中逐字节保留（同 `sha256`、同
    `size`）；
  - **replace**：仅 `old_index`，该旧块在新布局中不存在；
  - **new**：仅 `new_index`，该新块在旧对象中没有同内容块。
  - 行序固定：先全部 keep（按旧块序），再全部 replace（按旧块序），最后
    全部 new（按新块序）。

### 相同内容块的稳定标识

块的身份就是载荷的 SHA-256。当同一载荷在新旧布局中各出现多次（例如全零数据
被切成 9 个完全相同的 64 字节块）时，按**各布局内出现次序位置化配对**：第 k
个旧出现配第 k 个新出现，绝不把一个相同块错配到另一个副本。多出来配不上对的
旧出现记 `replace`，多出来的新出现记 `new`。

`diff_chunks(old_blocks, new_blocks)` 单独暴露这套配对规则，可用于任意两份
切分结果（例如持久化存储中的前后版本）。

### 复用语义与边界对齐口径

- 切分是因果的：完全位于编辑点之前（块结束位置 `<= offset`）的 keep 块在新
  布局中偏移逐字节不变。
- 32 字节滚动窗口完全越过编辑区后，新旧对象在相应位置上的窗口字节逐字节相同，
  因而窗口指纹严格相同（测试用独立长除参考逐位置核对这一解析关系）；块边界
  随之重新对齐。**严格的"边界平移锁定"没有与数据无关的常数上界**：例如整段
  常量数据只会在 256 字节处强制切块，编辑造成的相位差若非 256 的倍数，边界
  永不逐一对齐，但块载荷哈希仍相同，内容寻址复用照常成立。这正是复用以哈希
  而非偏移声明的原因。
- 本入口对替换后的对象做**整对象重扫**（题面允许），不保证局部重扫复杂度；
  不做持久版本/引用计数，也不内置块存储后端。

## 错误语义（可定位，不静默纠正）

异常都派生自 `rabin_cdc.CDCError`：

- `InvalidBatchTypeError`（同时是 `TypeError`）：`chunks` 不可迭代、直接传入
  单个 `bytes`、某批次不是 bytes-like、`memoryview` 非一维 `B` 格式。消息带
  批次下标（如 `batch 1: ...`）。`bool` 不会被当作整数批次接受；`float`
  （含 NaN/±Infinity）一律拒绝而非隐式转换。
- `InputTooLargeError`（同时是 `ValueError`）：累计输入会超过 1 MiB；
  `replace_region` 在替换结果会超过 1 MiB 时也抛此异常。
- `EditValidationError`（同时是 `ValueError`）：`replace_region` 的
  `offset` / `old_length` 不是真实 `int`（`bool` 拒绝、`float` 拒绝）、
  为负、越过对象尾，或区间半开关系非法。消息带具体数值与对象长度。
- `StreamFinishedError`：`finish()` 后再 `add()`/`finish()`。
- **有状态操作原子性**：类型与上限检查在消费任何字节之前完成；被拒批次不会
  部分进入内部状态，随后可继续喂入合法批次，结果与从未提交坏批次一致。
  `replace_region` 同样先完成全部参数校验再切分，且不修改传入对象；被拒
  调用不产生任何半更新结果，随后可用合法参数重试。
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
  `replace_region` 的替换结果同样不得超过 1 MiB。
- 非线程安全：一个 `Chunker` 不应被多线程并发 `add`。
- `replace_region` 对替换后的对象做整对象重扫，复杂度为 O(新对象长度)，
  没有局部重扫上界，也不做增量指纹状态接续（题面本轮不要求）。
- 块映射是单次编辑的内存结果：不维护跨版本的持久引用计数，不内置内容寻址
  块存储后端；`diff_chunks` 只依据两份切分的 SHA-256 配对。
- 内容寻址的“块存储后端/去重库”不在本题范围内，demo 只展示复用比例与映射。

## 仓库布局

```
rabin_cdc/
  __init__.py        公开接口导出（含本轮新增 replace_region / diff_chunks /
                     ReplacementResult / BlockMapping / KEEP/REPLACE/NEW）
  chunker.py         split_stream / Chunker / Block（滚动切分核心，本轮未改）
  diff.py            replace_region / diff_chunks / ReplacementResult /
                     BlockMapping（中间替换 + keep/replace/new 块映射）
  _fingerprint.py    GF(2) 余数与三张滚动更新表
  _errors.py         异常类型（本轮新增 EditValidationError）
tests/
  _slow_reference.py 独立慢速参考：逐位长除、每位置重算窗口（不导入被测实现）
  _data.py           确定性 LCG 样例数据（无外部附件）
  test_fingerprint.py 指纹/表/滚动与长除一致性
  test_chunker.py    契约、零数据、窗口前缀、任意分批等价、插入一字节恢复、
                     错误语义、原子性、上限、fuzz 对拍（原有断言全部保留）
  test_diff.py       中间替换：零数据手算向量、空对象/整删、首尾/跨边界编辑、
                     窗口指纹解析对齐、相同内容块位置化配对、fuzz 结构不变量、
                     错误类型/原子性、原入口对拍未变
demo.py              固定输入演示（切分复用比例 + 中间映射 + 相同内容块 +
                     首尾/空对象/整删边界 + 真实拒绝）
```

独立参考 `tests/_slow_reference.py` **不导入** `rabin_cdc`，期望值全部由原始
数学定义重新推出；“同一数据多种喂法结果一致”的往返检查仅作为补充测试。
