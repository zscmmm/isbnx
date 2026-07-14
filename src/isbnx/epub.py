"""EPUB ISBN 提取模块 — 只从文件内容扫描 ISBN，校验通过即返回。"""

from __future__ import annotations

import re
import time
import zipfile
from pathlib import Path
from typing import TYPE_CHECKING

from isbnx.config import settings
from isbnx.models import ExtractResult
from isbnx.utils.isbn_utils import BYTE_GATE, decode_bytes, extract_isbn

if TYPE_CHECKING:
    from isbnx.config import Settings
    from isbnx.detector import Detector


class EpubExtractor:
    """EPUB ISBN 提取器。

    提取策略按优先级：

    1. **OPF 元数据** — 解析 ``container.xml`` 定位 OPF，扫描 ``<dc:identifier>`` 等字段
    2. **XHTML 内容** — 版权页文件优先，扫描全文搜索 ISBN
    3. **封面/版权页图片** — 文件名以 ``cov``/``leg`` 开头的图片，ONNX 检测 + OCR

    纯文本扫描（无 OCR），通常 1-10ms 即可完成。
    """

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
        detector: Detector | None = None,
        filename: bool = False,
        config: Settings | None = None,
    ) -> ExtractResult:
        """从 EPUB 文件中提取 ISBN。

        优先解析 OPF 元数据，未命中时扫描 XHTML 文件内容，
        最后尝试封面/版权页图片的 ONNX 检测 + OCR。

        Args:
            epub_path: EPUB 文件路径。
            detector: 外部传入的 Detector 实例，为 ``None`` 时使用全局单例。
                仅当需要 ONNX 图片检测时才使用。
            filename: 是否优先从文件名中提取 ISBN。
            config: 可选的完整配置对象，为 ``None`` 时从全局 ``settings`` 读取。

        Returns:
            :class:`~isbnx.models.ExtractResult` 包含 ISBN 提取结果。
        """
        t0 = time.perf_counter()
        epub_path = Path(epub_path)
        _cfg = config or settings
        if not epub_path.exists():
            return ExtractResult.fail(str(epub_path), "epub", "EPUB 文件不存在", t0, strict=_cfg.strict)

        try:
            with zipfile.ZipFile(epub_path, "r") as zf:
                # 1. OPF 优先
                opf = cls._find_opf(zf)
                if opf:
                    isbn = cls._read_and_scan(zf, opf)
                    if isbn:
                        return ExtractResult.ok(str(epub_path), "epub", isbn, t0, strict=_cfg.strict)

                # 2. XHTML/HTML（版权页优先，限制总数）──
                files = cls._list_text_files(zf, skip=opf)
                for name in files:
                    isbn = cls._read_and_scan(zf, name)
                    if isbn:
                        return ExtractResult.ok(str(epub_path), "epub", isbn, t0, strict=_cfg.strict)

                # 3. 封面/版权页图片（文件名以 cov/leg 开头）──
                result = cls._scan_images(zf, str(epub_path), t0, detector=detector)
                if result:
                    return result

            return ExtractResult.fail(str(epub_path), "epub", "未找到有效 ISBN", t0, strict=_cfg.strict)
        except (OSError, zipfile.BadZipFile, RuntimeError) as e:
            return ExtractResult.fail(str(epub_path), "epub", f"EPUB 异常: {e}", t0, strict=_cfg.strict)

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
        except (KeyError, OSError, zipfile.BadZipFile):
            return None

        if not BYTE_GATE.search(raw):
            return None

        return extract_isbn(decode_bytes(raw))

    # ═════════════════════════════════════════════════════
    #  图片扫描（cov/leg 开头图片 → ONNX 检测 + OCR）
    # ═════════════════════════════════════════════════════

    @classmethod
    def _scan_images(
        cls, zf: zipfile.ZipFile, source: str, t0: float, *, detector: Detector | None = None
    ) -> ExtractResult | None:
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

        det = detector or get_detector()
        for name in image_names:
            try:
                raw = zf.read(name)
                img = load_image(raw)
                result = det.process(
                    img,
                    source=f"{source}!{name}",
                    source_type="epub",
                )
                if result.success:
                    result.elapsed = time.perf_counter() - t0
                    return result
            except (KeyError, OSError):
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
                return m.group(1).decode("utf-8")
        except (KeyError, OSError):
            pass
        return None
