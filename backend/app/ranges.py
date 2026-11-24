"""来源行区间工具：解析、格式化、合并、差集与行号展开。

区间用闭区间 [start, end] 表示一段来源数据行号，文本形式为 "1-8,13-14"，
单行写作 "5"，空区间统一显示为 "-"。采集缺口视图与补采清单都用这套表示。
"""
from __future__ import annotations

Range = tuple[int, int]

EMPTY_RANGE_TEXT = "-"


def normalize(ranges: list[Range]) -> list[Range]:
    """排序并合并重叠或相邻的区间。"""
    merged: list[Range] = []
    for start, end in sorted(ranges):
        if merged and start <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def parse_ranges(text: str) -> list[Range]:
    """把 "1-8,13-14" 解析成合并后的区间列表；空串、"-" 返回空列表。"""
    ranges: list[Range] = []
    for part in str(text or "").replace("，", ",").split(","):
        part = part.strip()
        if not part or part == EMPTY_RANGE_TEXT:
            continue
        if "-" in part:
            start_s, end_s = part.split("-", 1)
            start, end = int(start_s.strip()), int(end_s.strip())
        else:
            start = end = int(part)
        if start > end:
            raise ValueError(f"区间起点大于终点：{part}")
        ranges.append((start, end))
    return normalize(ranges)


def format_ranges(ranges: list[Range]) -> str:
    """把区间列表格式化成 "1-8,13-14"，空列表显示为 "-"。"""
    merged = normalize(ranges)
    if not merged:
        return EMPTY_RANGE_TEXT
    return ",".join(f"{s}-{e}" if s != e else f"{s}" for s, e in merged)


def subtract(a: list[Range], b: list[Range]) -> list[Range]:
    """区间差集：从 a 中扣掉 b 覆盖的行。"""
    left = normalize(a)
    right = normalize(b)
    result: list[Range] = []
    for start, end in left:
        cursor = start
        for b_start, b_end in right:
            if b_end < cursor:
                continue
            if b_start > end:
                break
            if b_start > cursor:
                result.append((cursor, b_start - 1))
            cursor = max(cursor, b_end + 1)
            if cursor > end:
                break
        if cursor <= end:
            result.append((cursor, end))
    return result


def ranges_to_rows(ranges: list[Range]) -> set[int]:
    """把区间展开成来源行号集合。"""
    rows: set[int] = set()
    for start, end in ranges:
        rows.update(range(start, end + 1))
    return rows


def rows_to_ranges(rows: set[int] | list[int]) -> list[Range]:
    """把行号集合压缩成合并后的区间列表。"""
    ordered = sorted(rows)
    if not ordered:
        return []
    ranges: list[Range] = []
    start = prev = ordered[0]
    for row in ordered[1:]:
        if row == prev + 1:
            prev = row
            continue
        ranges.append((start, prev))
        start = prev = row
    ranges.append((start, prev))
    return ranges


def row_count(ranges: list[Range]) -> int:
    """区间覆盖的行数。"""
    return sum(end - start + 1 for start, end in normalize(ranges))
