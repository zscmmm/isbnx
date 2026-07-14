"""MOBI ISBN 提取模块 — 只从文件内容扫描 ISBN，校验通过即返回。"""

from __future__ import annotations

import struct
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from loguru import logger

from isbnx.config import settings
from isbnx.models import ExtractResult
from isbnx.utils.isbn_utils import BYTE_GATE, decode_bytes, extract_isbn

if TYPE_CHECKING:
    from isbnx.config import Settings
    from isbnx.detector import Detector


@dataclass(frozen=True)
class _MobiLayout:
    compression: int
    text_records: int
    offsets: tuple[int, ...]
    exth_start: int | None
    text_start: int


class MobiExtractor:
    """MOBI ISBN 提取器。

    提取策略按优先级：

    1. **EXTH 元数据** — 从 MOBI 头部 EXTH 记录中提取 ISBN（type=104）
    2. **文本记录** — 遍历 MOBI 文本数据块，解码后扫描 ISBN

    支持 PalmDoc 压缩（compression=2）和无压缩（compression=1）格式。
    """

    # ═════════════════════════════════════════════════════
    #  public
    # ═════════════════════════════════════════════════════

    @classmethod
    def extract(
        cls,
        mobi_path: str | Path,
        *,
        detector: Detector | None = None,
        filename: bool = False,
        config: Settings | None = None,
    ) -> ExtractResult:
        """从 MOBI 文件中提取 ISBN。

        先扫描 EXTH 元数据中的 ISBN 字段，未命中时遍历文本记录块解码搜索。

        Args:
            mobi_path: MOBI 文件路径。
            detector: 外部传入的 Detector 实例（API 一致性保留，MOBI 内部不使用）。
            filename: 是否优先从文件名中提取 ISBN。
            config: 可选的完整配置对象，为 ``None`` 时从全局 ``settings`` 读取。

        Returns:
            :class:`~isbnx.models.ExtractResult` 包含 ISBN 提取结果。
        """
        t0 = time.perf_counter()
        mobi_path = Path(mobi_path)
        _cfg = config or settings
        if not mobi_path.exists():
            return ExtractResult.fail(str(mobi_path), "mobi", "MOBI 文件不存在", t0, strict=_cfg.strict)

        try:
            file_size = mobi_path.stat().st_size
            if file_size > 500 * 1024 * 1024:
                logger.warning(
                    "MOBI 文件较大（{} MB），全部读入内存可能消耗较多资源: {}", file_size // (1024 * 1024), mobi_path
                )
            data = mobi_path.read_bytes()
            layout = cls._parse_layout(data)
            if layout is None:
                return ExtractResult.fail(str(mobi_path), "mobi", "MOBI 文件格式错误或损坏", t0, strict=_cfg.strict)

            isbn = cls._isbn_from_exth(data, layout)
            if isbn:
                return ExtractResult.ok(str(mobi_path), "mobi", isbn, t0, strict=_cfg.strict)

            for chunk in cls._get_text_chunks(data, layout):
                if layout.compression == 2:
                    chunk = cls._decompress_palmdoc(chunk)
                elif layout.compression != 1:
                    continue

                if not BYTE_GATE.search(chunk):
                    continue

                isbn = extract_isbn(decode_bytes(chunk))
                if isbn:
                    return ExtractResult.ok(str(mobi_path), "mobi", isbn, t0, strict=_cfg.strict)

            return ExtractResult.fail(str(mobi_path), "mobi", "未找到有效 ISBN", t0, strict=_cfg.strict)
        except (OSError, struct.error, ValueError) as e:
            return ExtractResult.fail(str(mobi_path), "mobi", f"MOBI 异常: {e}", t0, strict=_cfg.strict)

    # ═════════════════════════════════════════════════════
    #  MOBI 结构解析
    # ═════════════════════════════════════════════════════

    @classmethod
    def _parse_layout(cls, data: bytes) -> _MobiLayout | None:
        if len(data) < 82:
            return None

        record_count = struct.unpack_from(">H", data, 76)[0]
        if record_count < 1:
            return None

        base = 78
        if len(data) < base + record_count * 8:
            return None

        offsets = tuple(struct.unpack_from(">L", data, base + i * 8)[0] for i in range(record_count))
        rec0_off = offsets[0]
        if len(data) < rec0_off + 16:
            return None

        compression, _, _text_len, text_records, _rec_size, _cur_pos = struct.unpack_from(">HHIHHI", data, rec0_off)
        text_records = max(1, min(text_records, record_count))

        mobi_off = rec0_off + 16
        if data[mobi_off : mobi_off + 4] != b"MOBI":
            return None

        header_len = struct.unpack_from(">L", data, mobi_off + 4)[0]
        exth_start = mobi_off + header_len
        text_start = exth_start
        if data[exth_start : exth_start + 4] == b"EXTH" and len(data) >= exth_start + 12:
            exth_len = struct.unpack_from(">L", data, exth_start + 4)[0]
            if exth_len >= 12 and exth_start + exth_len <= len(data):
                text_start = exth_start + exth_len

        return _MobiLayout(
            compression=compression,
            text_records=text_records,
            offsets=offsets,
            exth_start=exth_start if data[exth_start : exth_start + 4] == b"EXTH" else None,
            text_start=text_start,
        )

    @classmethod
    def _get_text_chunks(cls, data: bytes, layout: _MobiLayout) -> list[bytes]:
        chunks: list[bytes] = []
        if not layout.offsets:
            return chunks

        rec0_end = layout.offsets[1] if len(layout.offsets) > 1 else len(data)
        if layout.text_start < rec0_end:
            chunks.append(data[layout.text_start : rec0_end])

        for rec_idx in range(1, layout.text_records):
            if rec_idx >= len(layout.offsets):
                break
            start = layout.offsets[rec_idx]
            end = layout.offsets[rec_idx + 1] if rec_idx + 1 < len(layout.offsets) else len(data)
            if start < end:
                chunks.append(data[start:end])
        return chunks

    @classmethod
    def _isbn_from_exth(cls, data: bytes, layout: _MobiLayout) -> str | None:
        if layout.exth_start is None:
            return None

        if data[layout.exth_start : layout.exth_start + 4] != b"EXTH":
            return None

        exth_len = struct.unpack_from(">L", data, layout.exth_start + 4)[0]
        exth_end = layout.exth_start + exth_len
        if exth_len < 12 or exth_end > len(data):
            return None

        count = struct.unpack_from(">L", data, layout.exth_start + 8)[0]
        pos = layout.exth_start + 12
        for _ in range(count):
            if pos + 8 > exth_end:
                break
            rec_type = struct.unpack_from(">L", data, pos)[0]
            rec_size = struct.unpack_from(">L", data, pos + 4)[0]
            if rec_size < 8 or pos + rec_size > exth_end:
                break
            if rec_type == 104:
                raw = data[pos + 8 : pos + rec_size]
                isbn = extract_isbn(decode_bytes(raw))
                if isbn:
                    return isbn
            pos += rec_size
        return None

    # ═════════════════════════════════════════════════════
    #  解压
    # ═════════════════════════════════════════════════════

    @staticmethod
    def _decompress_palmdoc(data: bytes) -> bytes:
        out = bytearray()
        p = 0
        while p < len(data):
            c = data[p]
            p += 1
            if 1 <= c <= 8:
                if p + c > len(data):
                    break
                out.extend(data[p : p + c])
                p += c
            elif c < 128:
                out.append(c)
            elif c >= 192:
                out.extend((32, c ^ 128))
            else:
                if p >= len(data):
                    break
                c = (c << 8) | data[p]
                p += 1
                m = (c >> 3) & 0x07FF
                n = (c & 7) + 3
                if m == 0 or m > len(out):
                    break
                if m > n:
                    out.extend(out[-m : n - m])
                else:
                    for _ in range(n):
                        out.append(out[-m])
        return bytes(out)
