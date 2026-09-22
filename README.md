# cdc — 内容定义分块（抗边界扰动切分）

离线数据存储工程的可复用分块模块。纯 Python 标准库实现，从零编写，不依赖任何第三方同类实现。

- **Python 版本**：3.14.7（仅用标准库，`hashlib` / `dataclasses` / `math`）
- **额外依赖**：无。安装 Python 后即可运行，全部离线，无需账号、网络、数据库或 Docker。

## 接口

```python
from cdc import split_stream, Chunker, Block

blocks = split_stream(chunks)          # chunks: 可迭代的 bytes-like 序列（或单个 bytes）
# 或有状态用法：
c = Chunker()
blocks = c.update(chunk)               # 返回本次完成的块
tail   = c.finish()                    # 仅当尾块非空时返回一个块
```

`Block`（frozen dataclass）：`start`、`end`（绝对字节区间，end 排他）、`size`、`sha256`（十六进制摘要）、`reason`（`"fingerprint"` / `"max_size"` / `"end_of_stream"`）。

## 算法契约

- **指纹**：Rabin 型，GF(2) 多项式余数。窗口为最近 32 字节（流首不足 32 字节时左侧补零），首字节为最高位，模多项式 **x^16 + x^12 + x^5 + 1**（16 位指纹）。
- **切块规则**：每字节后，块长 ≥ 64 且指纹低 6 位为 0 → 切（`fingerprint`）；否则块长 = 256 → 强切（`max_size`）；`finish()` 输出非空尾块（`end_of_stream`）。
- **窗口跨块不重置**，始终在整个流上滑动；因此结果只取决于拼接后的字节流，与输入如何分块无关。
- **滚动更新表**：`mod_table`（移出字节约化）与 `out_table`（离窗字节贡献），每字节 O(1)，不为每个位置重算整个窗口。
- 参数（`min_size`/`max_size`/`window_size`/`mask`）可配置，但默认值即本题契约。

## 输入上限与错误语义

- 单条流总输入上限 **1 MiB**（`MAX_INPUT_BYTES`），超出抛 `InputTooLargeError`；检查在消费任何字节之前完成，**失败的 `update()` 不留下任何部分变更**。
- 整数参数拒绝 `bool`；拒绝 NaN/Infinity 及其他非 int 数值（`InvalidParameterError`，属 `ValueError`）。
- 非 bytes-like 的块抛 `InvalidChunkTypeError`（属 `TypeError`），错误信息含出错块的下标，可定位。
- `finish()` 后再 `update()`/`finish()` 抛 `StreamClosedError`。
- `split_stream` 失败时不返回任何部分结果。

## 设计取舍

- 纯 Python 逐字节滚动更新（约 1 MiB/秒量级），换取零依赖与确定性；块内容只保留当前块（≤256 字节）与 32 字节窗口，SHA-256 在切块时一次性计算。
- `cdc/reference.py` 是独立的慢速参考：每个位置用多项式长除从零重算指纹，不调用核心代码，仅供测试对照（O(n·窗口)，只适合测试规模）。
- 库本身不产生浮点结果。demo 的复用比例以精确分数 `a/b` 和百分比（保留 2 位小数，round-half-even）同时打印；百分比仅用于展示，精确值以分数为准。

## 已知限制

- 非线程安全；单流、单方向（不支持回退/重定位）。
- 仅实现本题契约的有限子集，不宣称与任何行业标准 CDC 实现（如 restic、LBFS 等）兼容。
- 参考实现只用于测试，不应用于大数据。

## 运行

```bash
python -m unittest discover -s tests -v   # 必要测试（含核心 vs 独立参考对照）
python demo.py                            # 约 1 秒内完成：正常结果 + 复用比例 + 一个实际触发的拒绝
```

## 布局

- `cdc/core.py` — 核心实现（滚动表、Chunker、split_stream、错误类型）
- `cdc/reference.py` — 独立慢速多项式长除参考（测试用）
- `tests/test_cdc.py` — 单元测试
- `demo.py` — 固定输入演示
