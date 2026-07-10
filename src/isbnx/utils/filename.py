"""从文件名中提取 ISBN / SSID 的工具函数。"""

from __future__ import annotations

from pathlib import Path

from cfunbook import extract_isbn, extract_ssid

from isbnx.config import settings
from isbnx.models import BookInfo


def extract_from_stem(
    stem: str,
    strict: int | None = None,
) -> BookInfo | None:
    """从文件名主干（不含扩展名）中提取 ISBN 和 SSID。

    与 :func:`extract_from_filename` 的区别是不做文件存在性检查，
    适用于调用方已确认文件存在的场景（如批处理扫描后）。

    Args:
        stem: 文件名主干（不含路径和后缀）。
        strict: 严格等级，见 ``BookInfo.is_valid()``。

    Returns:
        提取成功返回 ``BookInfo``，否则返回 None。
    """
    isbn = extract_isbn(stem) or None
    ssid_raw = extract_ssid(stem)
    ssid = str(ssid_raw) if ssid_raw is not None else None

    info = BookInfo(isbn=isbn, ssid=ssid)
    if info.is_valid(strict=strict if strict is not None else settings.strict):
        return info
    return None


def extract_from_filename(
    path: str | Path,
    strict: int | None = None,
) -> BookInfo | None:
    """从文件名（不含扩展名）中提取 ISBN 和 SSID。

    使用 ``cfunbook.extract_isbn`` 和 ``cfunbook.extract_ssid`` 提取，
    结合 ``strict`` 等级校验，有效则返回 ``BookInfo``，否则返回 None。

    Args:
        path: 文件路径。
        strict: 严格等级，见 ``BookInfo.is_valid()``。
            为 ``None`` 时使用全局 ``settings.strict``。

    Returns:
        提取成功返回 ``BookInfo``，否则返回 None（不在文件名中、或校验不通过）。
    """
    p = Path(path)
    if not p.exists():
        return None
    return extract_from_stem(p.stem, strict=strict)
