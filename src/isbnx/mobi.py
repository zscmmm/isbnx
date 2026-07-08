"""MOBI ISBN 提取模块 — 只从文件内容扫描 ISBN，校验通过即返回。"""

from __future__ import annotations

import re
import struct
import time
from dataclasses import dataclass
from pathlib import Path

from isbnx.models import BookInfo, ExtractResult, Meta


@dataclass(frozen=True)
class _MobiLayout:
    compression: int
    text_records: int
    offsets: list[int]
    exth_start: int | None
    text_start: int


class MobiExtractor:
    """MOBI ISBN 提取器。"""

    _BYTE_GATE = re.compile(rb"isbn|97[89][\d\- Xx]{10,}", re.IGNORECASE)
    _RE_ISBN_LABEL = re.compile(r"(?:ISBN(?:-1[03])?|isbn)[：:\s=]*([\dXx\- －—–]{10,})", re.IGNORECASE)
    _RE_ISBN_978 = re.compile(r"\b(97[89][\dXx\- －—–]{9,})\b")

    # ═════════════════════════════════════════════════════
    #  public
    # ═════════════════════════════════════════════════════

    @classmethod
    def extract(cls, mobi_path: str | Path) -> ExtractResult:
        t0 = time.perf_counter()
        mobi_path = Path(mobi_path)
        if not mobi_path.exists():
            return cls._fail(str(mobi_path), t0, "MOBI 文件不存在")

        try:
            data = mobi_path.read_bytes()
            layout = cls._parse_layout(data)
            if layout is None:
                return cls._fail(str(mobi_path), t0, "MOBI 文件格式错误或损坏")

            isbn = cls._isbn_from_exth(data, layout)
            if isbn:
                return cls._ok(str(mobi_path), t0, isbn)

            for chunk in cls._iter_text_chunks(data, layout):
                if layout.compression == 2:
                    chunk = cls._decompress_palmdoc(chunk)
                elif layout.compression != 1:
                    continue

                if not cls._BYTE_GATE.search(chunk):
                    continue

                isbn = cls._scan(cls._decode(chunk))
                if isbn:
                    return cls._ok(str(mobi_path), t0, isbn)

            return cls._fail(str(mobi_path), t0, "未找到有效 ISBN")
        except Exception as e:
            return cls._fail(str(mobi_path), t0, f"MOBI 异常: {e}")

    # ═════════════════════════════════════════════════════
    #  MOBI 结构解析
    # ═════════════════════════════════════════════════════

    @classmethod
    def _parse_layout(cls, data: bytes) -> _MobiLayout | None:
        try:
            if len(data) < 82:
                return None

            record_count = struct.unpack_from(">H", data, 76)[0]
            if record_count < 1:
                return None

            base = 78
            if len(data) < base + record_count * 8:
                return None

            offsets = [struct.unpack_from(">L", data, base + i * 8)[0] for i in range(record_count)]
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
        except Exception:
            return None

    @classmethod
    def _iter_text_chunks(cls, data: bytes, layout: _MobiLayout) -> list[bytes]:
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

        try:
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
                    isbn = cls._validate(cls._clean(cls._decode(raw)))
                    if isbn:
                        return isbn
                pos += rec_size
        except Exception:
            return None
        return None

    # ═════════════════════════════════════════════════════
    #  扫描 / 解压
    # ═════════════════════════════════════════════════════

    @classmethod
    def _scan(cls, text: str) -> str | None:
        for m in cls._RE_ISBN_LABEL.finditer(text):
            v = cls._validate(cls._clean(m.group(1)))
            if v:
                return v
        for m in cls._RE_ISBN_978.finditer(text):
            v = cls._validate(cls._clean(m.group(1)))
            if v:
                return v
        return None

    @staticmethod
    def _decompress_palmdoc(data: bytes) -> bytes:
        out = bytearray()
        p = 0
        while p < len(data):
            c = data[p]
            p += 1
            if 1 <= c <= 8:
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

    # ═════════════════════════════════════════════════════
    #  helpers
    # ═════════════════════════════════════════════════════

    @staticmethod
    def _clean(raw: str) -> str:
        return re.sub(r"[^0-9Xx]", "", raw).upper()

    @staticmethod
    def _validate(candidate: str) -> str | None:
        if len(candidate) not in (10, 13):
            return None
        try:
            from mneia_isbn import ISBN as _ISBN

            obj = _ISBN(candidate)
            return str(obj.as_isbn13) if obj.is_valid else None
        except Exception:
            return None

    @staticmethod
    def _decode(data: bytes) -> str:
        for enc in ("utf-8", "gb18030", "big5"):
            try:
                return data.decode(enc)
            except (UnicodeDecodeError, UnicodeError):
                continue
        return data.decode("utf-8", errors="ignore")

    @staticmethod
    def _ok(source: str, t0: float, isbn: str) -> ExtractResult:
        return ExtractResult(
            bookinfo=BookInfo(isbn=isbn),
            meta=Meta(source=source, source_type="mobi"),
            elapsed=time.perf_counter() - t0,
        )

    @staticmethod
    def _fail(source: str, t0: float, msg: str) -> ExtractResult:
        return ExtractResult(
            bookinfo=BookInfo(),
            meta=Meta(source=source, source_type="mobi"),
            error=msg,
            elapsed=time.perf_counter() - t0,
        )
