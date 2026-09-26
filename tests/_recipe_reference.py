"""Independent tiny reference for recipe tests; it does not import rabin_cdc."""

from __future__ import annotations


def apply_recipe_reference(instructions, old_blocks):
    out = bytearray()
    for instruction in instructions:
        kind = instruction[0]
        if kind == "literal":
            out.extend(instruction[1])
        elif kind == "copy":
            _, block_index, start, end = instruction
            if block_index < 0 or block_index >= len(old_blocks):
                raise IndexError("bad block index")
            if start < 0 or end < start or end > len(old_blocks[block_index]):
                raise IndexError("bad block slice")
            out.extend(old_blocks[block_index][start:end])
        else:
            raise ValueError("unknown instruction")
    return bytes(out)


def _earliest_copy(segment, blocks):
    for block_index, payload in enumerate(blocks):
        start = payload.find(segment)
        if start >= 0:
            return (0, block_index, start, start + len(segment))
    return None


def _best_partition(segments, blocks, target, best):
    instructions = []
    literal_bytes = 0
    cursor = 0
    for segment in segments:
        copy_key = _earliest_copy(segment, blocks)
        if copy_key is None:
            key = (1, segment)
            instruction = ("literal", segment)
            literal_bytes += len(segment)
        else:
            key = copy_key
            instruction = (
                "copy",
                copy_key[1],
                copy_key[2],
                copy_key[3],
            )
        instructions.append(instruction)
        cursor += len(segment)
    if cursor != len(target):
        raise AssertionError("partition does not tile target")
    order = [
        (
            (0, instruction[1], instruction[2], instruction[3])
            if instruction[0] == "copy"
            else (1, instruction[1])
        )
        for instruction in instructions
    ]
    candidate = (literal_bytes, len(segments), order, tuple(instructions))
    if best is None or candidate[:3] < best[:3]:
        return candidate
    return best


def enumerate_best_recipe(target, blocks):
    """Enumerate every cut vector and return the independently best recipe."""

    best = None

    def visit(start, segments):
        nonlocal best
        if start == len(target):
            best = _best_partition(tuple(segments), blocks, target, best)
            return
        for end in range(start + 1, len(target) + 1):
            segments.append(target[start:end])
            visit(end, segments)
            segments.pop()

    visit(0, [])
    if best is None:
        return (0, ())
    return (best[0], best[3])
