"""Independent reference for recipe tests: does NOT import rabin_cdc.

Everything here is re-derived from the task statement: a recipe is a
sequence of copy ops (block, start, length) and literal ops (bytes);
executing it concatenates the referenced bytes.  Optimality is checked by
brute force over the raw old bytes, never through the library under test.
"""

from __future__ import annotations


def to_tuple_ops(recipe):
    """Duck-typed conversion of library ops to plain tuples (no import)."""
    out = []
    for op in recipe:
        if hasattr(op, "data"):
            out.append(("lit", bytes(op.data)))
        else:
            out.append(("copy", op.block, op.start, op.length))
    return out


def interpret(old_blocks, tuple_ops):
    """Independent executor: concatenates referenced bytes, checks ranges."""
    out = bytearray()
    for op in tuple_ops:
        if op[0] == "lit":
            out += op[1]
        else:
            _, block, start, length = op
            piece = old_blocks[block][start : start + length]
            assert len(piece) == length, "copy range outside block"
            out += piece
    return bytes(out)


def cost_of(tuple_ops):
    literal = sum(len(op[1]) for op in tuple_ops if op[0] == "lit")
    return (literal, len(tuple_ops))


def optimal_cost(old_blocks, new):
    """Brute-force exact optimum of (literal_bytes, op_count).

    Scans the raw old bytes for matches at every position; no digests, no
    shared code with the library.
    """
    n = len(new)
    best = [(0, 0)] * (n + 1)
    for i in range(n - 1, -1, -1):
        options = []
        for length in range(1, n - i + 1):
            lit, ops = best[i + length]
            options.append((lit + length, ops + 1))
        for block in old_blocks:
            for start in range(len(block)):
                span = 0
                while (
                    start + span < len(block)
                    and i + span < n
                    and block[start + span] == new[i + span]
                ):
                    span += 1
                for length in range(1, span + 1):
                    lit, ops = best[i + length]
                    options.append((lit, ops + 1))
        best[i] = min(options)
    return best[0]


def enumerate_recipes(old_blocks, new, max_literal, max_ops):
    """Yield every feasible recipe (tuple-op lists) within the cost bound."""
    n = len(new)

    def rec(i, budget_lit, budget_ops):
        if i == n:
            yield []
            return
        if budget_ops == 0:
            return
        for length in range(1, min(n - i, budget_lit) + 1):
            for rest in rec(i + length, budget_lit - length, budget_ops - 1):
                yield [("lit", new[i : i + length])] + rest
        for b, block in enumerate(old_blocks):
            for start in range(len(block)):
                span = 0
                while (
                    start + span < len(block)
                    and i + span < n
                    and block[start + span] == new[i + span]
                ):
                    span += 1
                for length in range(1, span + 1):
                    for rest in rec(i + length, budget_lit, budget_ops - 1):
                        yield [("copy", b, start, length)] + rest

    yield from rec(0, max_literal, max_ops)
