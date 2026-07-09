"""EPUB ISBN 提取模块 — 只从文件内容扫描 ISBN，校验通过即返回。"""

from __future__ import annotations

import re
import time
import zipfile
from pathlib import Path

from isbnx.models import BookInfo, ExtractResult, Meta
from isbnx.utils.filename import extract_from_filename


class EpubExtractor:
    """EPUB ISBN 提取器。"""

    # ── 字节级预过滤 ──
    _BYTE_GATE = re.compile(rb"isbn|97[89][\d\- Xx]{10,}", re.IGNORECASE)

    # ── 文本扫描正则 ──
    _RE_ISBN_LABEL = re.compile(r"(?:ISBN(?:-1[03])?|isbn)[：:\s=]*([\dXx\- －—–]{10,})", re.IGNORECASE)
    _RE_ISBN_978 = re.compile(r"\b(97[89][\dXx\- －—–]{9,})\b")

    _TEXT_EXTS = (".opf", ".xhtml", ".html", ".htm", ".xml")
    _MAX_BYTES = 10 * 1024 * 1024
    _MAX_SCAN = 200

    # ── 图片文件名前缀（封面/版权页图片优先扫描）──
    _IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".bmp")
    _IMAGE_PREFIXES = ("cov", "leg")
    _MAX_IMAGES = 10

    # ── 版权页文件名关键词（这些文件优先扫描）──
    _FRONT_KEYS = (
        "copyright",
        "copyr",
        "titlepage",
        "title",
        "front",
        "colophon",
        "imprint",
        "verso",
        "credits",
        "leg001",
        "leg0001",
        "colop",
    )

    # ═════════════════════════════════════════════════════
    #  public
    # ═════════════════════════════════════════════════════

    @classmethod
    def extract(
        cls,
        epub_path: str | Path,
        *,
        filename: bool = False,
    ) -> ExtractResult:
        t0 = time.perf_counter()
        epub_path = Path(epub_path)
        if not epub_path.exists():
            return cls._fail(str(epub_path), t0, "EPUB 文件不存在")

        if filename:
            info = extract_from_filename(epub_path)
            if info:
                return ExtractResult(
                    bookinfo=info,
                    meta=Meta(source=str(epub_path), source_type="epub"),
                    elapsed=0.0,
                )

        try:
            with zipfile.ZipFile(epub_path, "r") as zf:
                # 1. OPF 优先
                opf = cls._find_opf(zf)
                if opf:
                    isbn = cls._read_and_scan(zf, opf)
                    if isbn:
                        return cls._ok(str(epub_path), t0, isbn)

                # 2. XHTML/HTML（版权页优先，限制总数）──
                files = cls._list_text_files(zf, skip=opf)
                for name in files:
                    isbn = cls._read_and_scan(zf, name)
                    if isbn:
                        return cls._ok(str(epub_path), t0, isbn)

                # 3. 封面/版权页图片（文件名以 cov/leg 开头）──
                result = cls._scan_images(zf, str(epub_path), t0)
                if result:
                    return result

            return cls._fail(str(epub_path), t0, "未找到有效 ISBN")
        except Exception as e:
            return cls._fail(str(epub_path), t0, f"EPUB 异常: {e}")

    # ═════════════════════════════════════════════════════
    #  文件列表（版权页优先）
    # ═════════════════════════════════════════════════════

    @classmethod
    def _list_text_files(cls, zf: zipfile.ZipFile, skip: str | None) -> list[str]:
        """收集文本文件，版权页关键词命中者排前面，总数受 _MAX_SCAN 限制。"""
        front: list[str] = []
        rest: list[str] = []
        for name in zf.namelist():
            if name == skip:
                continue
            if not name.lower().endswith(cls._TEXT_EXTS):
                continue
            stem = Path(name).stem.lower()
            if any(k in stem for k in cls._FRONT_KEYS):
                front.append(name)
            else:
                rest.append(name)
        front.extend(rest)
        return front[: cls._MAX_SCAN]

    # ═════════════════════════════════════════════════════
    #  读取 + 扫描
    # ═════════════════════════════════════════════════════

    @classmethod
    def _read_and_scan(cls, zf: zipfile.ZipFile, name: str) -> str | None:
        try:
            info = zf.getinfo(name)
            if info.file_size > cls._MAX_BYTES:
                return None
            raw = zf.read(name)
        except Exception:
            return None

        if not cls._BYTE_GATE.search(raw):
            return None

        return cls._scan(cls._decode(raw))

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

    # ═════════════════════════════════════════════════════
    #  图片扫描（cov/leg 开头图片 → ONNX 检测 + OCR）
    # ═════════════════════════════════════════════════════

    @classmethod
    def _scan_images(cls, zf: zipfile.ZipFile, source: str, t0: float) -> ExtractResult | None:
        """扫描 EPUB 中文件名以 cov/leg 开头的图片，通过 ONNX 检测 + OCR 提取 ISBN。"""
        # 收集符合条件的图片，cov 优先于 leg
        cov: list[str] = []
        leg: list[str] = []
        for name in zf.namelist():
            stem = Path(name).stem.lower()
            if not name.lower().endswith(cls._IMAGE_EXTS):
                continue
            if stem.startswith("cov"):
                cov.append(name)
            elif stem.startswith("leg"):
                leg.append(name)
        image_names = (cov + leg)[: cls._MAX_IMAGES]
        if not image_names:
            return None

        from isbnx.detector import get_detector
        from isbnx.utils.io import load_image

        detector = get_detector()
        for name in image_names:
            try:
                raw = zf.read(name)
                img = load_image(raw)
                result = detector.process(
                    img,
                    source=f"{source}!{name}",
                    source_type="epub",
                )
                if result.success:
                    result.elapsed = time.perf_counter() - t0
                    return result
            except Exception:
                continue
        return None

    # ═════════════════════════════════════════════════════
    #  OPF 定位
    # ═════════════════════════════════════════════════════

    @staticmethod
    def _find_opf(zf: zipfile.ZipFile) -> str | None:
        try:
            data = zf.read("META-INF/container.xml")
            m = re.search(rb'full-path\s*=\s*"([^"]+)"', data)
            if m:
                return m.group(1).decode("ascii")
        except Exception:
            pass
        return None

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
        for enc in ("utf-8", "utf-16-le", "gb18030", "big5"):
            try:
                return data.decode(enc)
            except (UnicodeDecodeError, UnicodeError):
                continue
        return data.decode("utf-8", errors="ignore")

    @staticmethod
    def _ok(source: str, t0: float, isbn: str) -> ExtractResult:
        return ExtractResult(
            bookinfo=BookInfo(isbn=isbn),
            meta=Meta(source=source, source_type="epub"),
            elapsed=time.perf_counter() - t0,
        )

    @staticmethod
    def _fail(source: str, t0: float, msg: str) -> ExtractResult:
        return ExtractResult(
            bookinfo=BookInfo(),
            meta=Meta(source=source, source_type="epub"),
            error=msg,
            elapsed=time.perf_counter() - t0,
        )
