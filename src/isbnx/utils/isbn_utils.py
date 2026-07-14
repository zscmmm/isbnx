"""ISBN 字符串提取与基础校验工具。"""

from __future__ import annotations

import re
import unicodedata

from mneia_isbn import ISBN as _ISBN

# ── 公共常量 ──────────────────────────────────────────

# ISBN 关键词匹配（OCR 常见误识处理：1SBN/IS8N/I5BN 等）
ISBN_KEYWORD = re.compile(r"[1Il]\s*[S5]\s*[8B]\s*N", re.IGNORECASE)

# 字节级预过滤：快速判断二进制数据中是否可能包含 ISBN
BYTE_GATE = re.compile(rb"isbn|97[89][\d\- Xx]{10,}", re.IGNORECASE)

# ── 匹配模式 ──────────────────────────────────────────
# 思路：宽松匹配候选项 → _clean_isbn() 清洗 → _is_valid() 校验
#
# 1. 优先找 "ISBN" 标记后的内容（含常见 OCR 误识 1SBN/IS8N）
# 2. 无标记时回退到 978/979 开头的长数字序列
# 3. 最终兜底：全文清洗后直接校验

# 数字之间的 CJK 字符（PDF CID 字体 ToUnicode CMap 错误导致的乱码）替换为分隔符
# 例如：ISBN 978唱7唱03唱027084唱9 → ISBN 978-7-03-027084-9
# 限制最多 3 个 CJK 字符（乱码通常为单字，避免误伤正常中文长句）
# 注意：必须放在 NFKC 规范化之后、ISBN 正则匹配之前
_CJK_BETWEEN_DIGITS = re.compile(r"(?<=\d)\s*[\u4e00-\u9fff]{1,3}\s*(?=\d)")

_ISBN_MARKER = re.compile(
    r"(?:[1Il]\s*[S5]\s*[8B]\s*N\s*[:：]?\s*)?"
    r"([\d \-–—]{6,}[\dXx])",
    re.IGNORECASE,
)

_ISBN_FALLBACK = re.compile(
    r"(?<!\d)(97[89][\d \-–—]{4,}\d)(?!\d)",
    re.IGNORECASE,
)


# ── 公共工具函数 ──────────────────────────────────────


def validate_and_format(raw: str) -> str | None:
    """清洗并校验 ISBN 候选字符串，返回 ISBN-13 或 None。

    合并了 EPUB/MOBI 模块中的 ``_clean()`` + ``_validate()`` 逻辑：
    先去除非数字字符，再校验长度和校验和。

    Args:
        raw: 可能含分隔符、全角字符的原始 ISBN 字符串。

    Returns:
        有效的 ISBN-13 字符串，或 ``None``。
    """
    cleaned = re.sub(r"[^0-9Xx]", "", raw).upper()
    if len(cleaned) not in (10, 13):
        return None
    try:
        obj = _ISBN(cleaned)
        return str(obj.as_isbn13) if obj.is_valid else None
    except Exception:  # noqa: BLE001
        return None


def decode_bytes(data: bytes) -> str:
    """尝试多种编码解码字节数据为文本。

    依次尝试 UTF-8 → UTF-16-LE → GB18030 → Big5，
    全部失败时用 UTF-8 + errors='ignore' 兜底。

    Args:
        data: 待解码的字节数据。

    Returns:
        解码后的文本字符串。
    """
    for enc in ("utf-8", "utf-16-le", "gb18030", "big5"):
        try:
            return data.decode(enc)
        except (UnicodeDecodeError, UnicodeError):
            continue
    return data.decode("utf-8", errors="ignore")


# ── 清洗与校验 ────────────────────────────────────────


def _normalize_text(text: str) -> str:
    """文本规范化：全角→半角 + PDF CID 字体乱码修复。"""
    text = unicodedata.normalize("NFKC", text)
    text = _CJK_BETWEEN_DIGITS.sub("-", text)
    return text


def _clean_isbn(raw: str) -> str:
    """只保留 ASCII 数字和末尾 X/x，统一转大写。"""
    return re.sub(r"[^0-9Xx]", "", raw).upper()


def is_valid_isbn(cleaned: str, *, china_only: bool = True) -> bool:
    """校验清洗后的字符串是否为合法 ISBN 格式。

    除基本的 ISBN 校验和检查外，还会拒绝以下伪 ISBN：

    * **CIP 号**：10 位数字以 ``20`` 开头（形如 ``2015101632``），
      这是中国 CIP （图书在版编目）编号格式，不是 ISBN。

    Args:
        cleaned: 纯数字/X 的 ISBN 字符串（不含分隔符）。
        china_only: 是否限制 ISBN-10 必须以 ``7``（中国组号）开头。
            默认 ``True``（仅接受中国大陆出版书籍）。
            设为 ``False`` 时接受任意组号的 ISBN。

    Returns:
        符合 ISBN 格式时返回 True。
    """
    try:
        if len(cleaned) not in (10, 13):
            return False
        # ISBN-13 必须以 978/979 开头（全球标准前缀）
        if len(cleaned) == 13 and not cleaned.startswith(("978", "979")):
            return False
        # ISBN-10 必须以 7 开头（中国组号）。
        # 本项目仅面向中国大陆出版书籍，组号 7 对应中国。
        # 其他组号（如 0/1=英语区、2=法语区、3=德语区、4=日本等）会被拒绝。
        # 如需支持外国书籍，传入 china_only=False 即可。
        if china_only and len(cleaned) == 10 and not cleaned.startswith("7"):
            return False
        return _ISBN(cleaned).is_valid
    except Exception:  # noqa: BLE001
        return False


# ── 公开 API ──────────────────────────────────────────


def extract_isbn(text: str) -> str | None:
    """从文本中提取第一条符合 ISBN 格式的字符串。

    三级匹配策略：

    1. **优先** 找 ``"ISBN"`` 标记后的内容（含 OCR 误识 ``1SBN/IS8N``）
    2. **回退** 到 ``978/979`` 开头的长短数字序列（无标记）
    3. **兜底** 全文清洗后扫描合法 ISBN-13/ISBN-10 子串

    匹配前会自动执行条形码 OCR 纠错（``1787`` → ``9787``，竖线被误读为 1）。

    Args:
        text: 可能包含 ISBN 的原始文本（单行或多行均可）。

    Returns:
        提取到的 ISBN 字符串（纯数字/X，不含分隔符），或 ``None``。

    Example:

        >>> extract_isbn("ISBN 978-7-89446-541-2")
        '9787894465412'
        >>> extract_isbn("2024年出版 978-7-89446-541-2")
        '9787894465412'
    """
    # 0. 文本规范化：全角→半角 + PDF 编码乱码修复
    text = _normalize_text(text)

    # 0a. 条形码 OCR 纠错：竖线被误读为 1（1787→9787）
    text = re.sub(r"(?<!\d)1787(\d)", r"9787\1", text)

    # 1. 优先找 "ISBN" 标记后的内容
    for match in _ISBN_MARKER.finditer(text):
        cleaned = _clean_isbn(match.group(1))
        if is_valid_isbn(cleaned):
            return cleaned

    # 2. 回退到 978/979 开头无标记候选
    for match in _ISBN_FALLBACK.finditer(text):
        cleaned = _clean_isbn(match.group(1))
        if is_valid_isbn(cleaned):
            return cleaned

    # 3. 兜底：在原文中提取接近 ISBN 格式的数字序列（允许短横线/空格分隔）
    #    逐一清洗校验。不同于全文本清洗后扫描——不会将散落各处的数字
    #    拼接成假阳性 ISBN（如 hash ``7Rys...0c03l78...35+6...`` 中的
    #    散落数字 ``7+0+03+78+0+35+6`` 不会聚合成 ``7003780356``）。
    for m in re.finditer(r"[\d\-–—\sXx]{8,}", text):
        candidate = _clean_isbn(m.group())
        if len(candidate) in (10, 13) and is_valid_isbn(candidate):
            return candidate

    return None


def extract_isbn_from_lines(lines: list[str]) -> str | None:
    """从 OCR 文本行列表中提取 ISBN。

    依次尝试：单行提取 → 相邻两行拼接 → 全部行拼接。

    Args:
        lines: OCR 识别的文本行列表。

    Returns:
        提取到的 ISBN 字符串，或 ``None``。
    """
    for line in lines:
        result = extract_isbn(line)
        if result is not None:
            return result
    # 尝试相邻行两两拼接（处理 ISBN 被 OCR 跨行截断的情况）
    for i in range(len(lines) - 1):
        combined = lines[i] + lines[i + 1]
        result = extract_isbn(combined)
        if result is not None:
            return result
        combined = lines[i] + " " + lines[i + 1]
        result = extract_isbn(combined)
        if result is not None:
            return result
    # 最后尝试把所有行拼接后提取（用空格分隔，避免跨行数字拼接成假 ISBN）
    return extract_isbn(" ".join(lines))
