"""
CIP 版权页字段提取规则（从 cipx 移植）。

针对 "图书在版编目 (CIP) 数据" 类页面，从 OCR 文本行中提取
书名、作者、出版社、出版日期、ISBN、CIP 核字号。
"""

from __future__ import annotations

import re

from isbnx.models import BookInfo
from isbnx.utils.isbn_utils import is_valid_isbn as _is_valid_isbn

# ── ISBN 提取 ──────────────────────────────────────────


def extract_isbn(lines: list[str]) -> str | None:
    """从文本行中提取 ISBN。

    先对各行做 CIP 特定的规范化（全角→半角、标点统一），
    再委托 ``isbn_utils.extract_isbn_from_lines`` 完成核心提取。

    Args:
        lines: 文本行列表。

    Returns:
        纯数字 ISBN 字符串，未找到时返回 None。
    """
    from isbnx.utils.isbn_utils import extract_isbn_from_lines as _extract_from_lines

    normalized = [_normalize_line(line) for line in lines]
    return _extract_from_lines(normalized)


# ── CIP 核字号提取 ────────────────────────────────────


def extract_cip(lines: list[str]) -> str | None:
    """从文本行中提取 CIP 数据核字号。"""
    full_text = " ".join(_normalize_line(line) for line in lines)

    # 格式1: CIP数据核字（2021）第188747号
    m = re.search(
        r"C[IT]P\s*数据核字\s*[（(]\s*(\d{4})\s*[）)]\s*第\s*([A-Z0-9]+)\s*号",
        full_text,
        re.IGNORECASE,
    )
    if m:
        return f"CIP数据核字({m.group(1)})第{m.group(2)}号"

    # 格式1.1: 缺"第"字
    m = re.search(
        r"C[IT]P\s*数据核字\s*[（(]\s*(\d{4})\s*[）)]\s*([A-Z0-9]+)\s*号",
        full_text,
        re.IGNORECASE,
    )
    if m:
        return f"CIP数据核字({m.group(1)})第{m.group(2)}号"

    # 格式2: CIP数据核字第XXXXXX号
    m = re.search(r"C[IT]P\s*数据核字第\s*([A-Z0-9]+)\s*号", full_text, re.IGNORECASE)
    if m:
        return f"CIP数据核字第{m.group(1)}号"

    # 格式3: 同行有 CIP + 核字
    for line in lines:
        if re.search(r"C[IT]P", line, re.IGNORECASE) and "核字" in line:
            m = re.search(r"核字第?\s*([A-Z0-9]+)\s*号", line, re.IGNORECASE)
            if m:
                return f"CIP数据核字第{m.group(1)}号"

    return None


# ── 主提取入口 ────────────────────────────────────────


def extract_cip_fields(lines: list[str]) -> BookInfo:
    """从 CIP 版权页 OCR 文本行中提取字段。

    优先解析标准 CIP 格式（"图书在版编目" 头部），
    不匹配时回退到通用正则提取。

    Args:
        lines: OCR 识别的文本行列表（已清洗去空）。

    Returns:
        ``BookInfo``，至少填充 ISBN（如有），其余字段暂存于额外字段。
    """
    lines = [_normalize_line(line) for line in lines if _normalize_line(line)]
    if not lines:
        return BookInfo()

    # 定位 CIP 头部行
    cip_idx = -1
    for i, line in enumerate(lines):
        if "图书在版编目" in line or "CIPPage" in line:
            cip_idx = i
            break

    if cip_idx == -1 or cip_idx + 1 >= len(lines):
        return _extract_generic(lines)

    # ── ISBN（全局搜索，不受上下文限制）──
    isbn = extract_isbn(lines)

    if isbn:
        return BookInfo(isbn=isbn)

    # 没提取到 ISBN 时，尝试通用回退
    return _extract_generic(lines)


# ── 通用回退 ──────────────────────────────────────────


def _extract_generic(lines: list[str]) -> BookInfo:
    """CIP 标准格式不匹配时的通用回退。"""
    text = "\n".join(lines)
    clean_text = re.sub(r"\s+", " ", text).strip()

    isbn = extract_isbn(lines)
    if isbn:
        return BookInfo(isbn=isbn)

    # 兜底：全文扫 978/979 序列
    for m in re.finditer(r"97[89][\d\-–—\s]{4,}\d", clean_text):
        cleaned = re.sub(r"[^0-9Xx]", "", m.group()).upper()
        if len(cleaned) in (10, 13) and _is_valid_isbn(cleaned):
            return BookInfo(isbn=cleaned)

    return BookInfo()


# ── 工具函数 ──────────────────────────────────────────


def _normalize_line(line: str) -> str:
    line = str(line).strip()
    line = re.sub(r"^#+\s*", "", line)
    line = line.replace("／", "/")
    line = line.replace("（", "(").replace("）", ")")
    line = line.replace("．", ".")
    line = line.replace("—", "-").replace("－", "-").replace("–", "-")
    line = re.sub(r"\s+", " ", line)
    return line.strip()


def _extract_isbn_from_window(value: str, require_prefix: bool = False) -> str | None:
    pattern = r"97[89][0-9Xx\s-]{9,24}[0-9Xx]" if require_prefix else r"[0-9Xx][0-9Xx\s-]{8,24}[0-9Xx]"
    for match in re.finditer(pattern, value):
        token = match.group(0)
        cleaned = re.sub(r"[^0-9Xx]", "", token).upper()
        if _is_valid_isbn(cleaned):
            return cleaned
    return None
