"""ISBN 提取入口，提供统一的提取接口。"""

from __future__ import annotations

from pathlib import Path
from threading import Lock
from typing import TYPE_CHECKING

from isbnx.config import settings
from isbnx.models import BookInfo, ExtractResult, Meta
from isbnx.utils.filename import extract_from_filename
from isbnx.utils.io import detect_file_kind, require_suffix

if TYPE_CHECKING:
    from isbnx.config import PDFConfig, Settings
    from isbnx.detector import Detector


class ISBNX:
    """ISBN 提取器统一入口。

    根据来源类型选择对应方法，所有方法返回统一的 :class:`~isbnx.models.ExtractResult`。

    用法示例::

        from isbnx import ISBNX

        # 从图片提取
        result = ISBNX().from_image("cover.png")
        if result.success:
            print(result.bookinfo.isbn13)  # 9787123456789

        # 从 PDF 提取
        result = ISBNX().from_pdf("book.pdf")

        # 自定义配置
        from isbnx.config import Settings

        config = Settings(strict=2)
        config.ocr.ocr_model = "medium"
        result = ISBNX(config=config).from_image("cover.png")
    """

    def __init__(self, config: Settings | None = None) -> None:
        self.config = config or settings
        self._detector: Detector | None = None
        self._detector_lock = Lock()

    @property
    def detector(self) -> Detector:
        """获取检测器（懒加载，首次访问时预热 ONNX + OCR）。

        使用实例自身的 ``config`` 创建检测器，避免依赖全局 ``settings`` 单例。
        """
        if self._detector is None:
            with self._detector_lock:
                if self._detector is None:
                    from isbnx.detector import Detector

                    self._detector = Detector(self.config)
        return self._detector

    # ── 单张图片 ──

    def from_image(
        self,
        filepath: str | Path,
        page: int = 1,
        *,
        filename: bool = False,
    ) -> ExtractResult:
        """从单张图片文件中提取 ISBN。

        支持常见图片格式（PNG/JPG/WebP/BMP 等）以及加密或非加密的 PDG 文件。
        PDG 文件会自动尝试解密后提取。

        Args:
            filepath: 图片文件路径。
            page: 页码（预留参数，仅兼容单页图片）。
            filename: 是否优先从文件名中提取 ISBN。

        Returns:
            :class:`~isbnx.models.ExtractResult` 包含 ISBN 提取结果。
        """
        filepath = Path(filepath)

        if filename:
            info = extract_from_filename(filepath, strict=self.config.strict)
            if info:
                return ExtractResult(
                    bookinfo=info,
                    meta=Meta(source=str(filepath), source_type="image"),
                    elapsed=0.0,
                    from_filename=True,
                    strict=self.config.strict,
                )

        if page != 1:
            raise NotImplementedError("仅支持单页图片")

        if filepath.suffix.lower() == ".pdg":
            from isbnx.archive import _pdg_to_image

            data = filepath.read_bytes()
            image = _pdg_to_image(data)
            if image is None:
                return ExtractResult(
                    bookinfo=BookInfo(),
                    meta=Meta(source=str(filepath), source_type="image"),
                    error="PDG 文件解码失败",
                    strict=self.config.strict,
                )
        else:
            from isbnx.utils.io import load_image

            image = load_image(filepath)

        return self.detector.process(image, source=str(filepath), source_type="image")

    def extract(
        self,
        filepath: str | Path,
        page: int = 1,
        *,
        filename: bool = False,
        pdf_config: PDFConfig | None = None,
    ) -> ExtractResult:
        """根据文件后缀自动选择对应的提取方法。

        Args:
            filepath: 文件路径。
            page: 页码，仅对图片有效。
            filename: 是否优先从文件名中提取 ISBN。
            pdf_config: 可选的 PDF 页码配置覆盖，为 ``None`` 时从全局 ``settings.pdf`` 读取。

        Returns:
            :class:`~isbnx.models.ExtractResult` 包含 ISBN 提取结果。
        """
        kind = detect_file_kind(filepath)
        if kind == "image":
            return self.from_image(filepath, page=page, filename=filename)
        if kind == "pdf":
            return self.from_pdf(filepath, filename=filename, pdf_config=pdf_config)
        if kind == "epub":
            return self.from_epub(filepath, filename=filename)
        if kind == "mobi":
            return self.from_mobi(filepath, filename=filename)
        return self.from_archive(filepath, filename=filename)

    # ── PDF ──

    def from_pdf(
        self,
        filepath: str | Path,
        *,
        filename: bool = False,
        pdf_config: PDFConfig | None = None,
    ) -> ExtractResult:
        """从 PDF 文件中提取 ISBN。

        支持文本型 PDF（文本搜索）和扫描件（渲染为图片后 ONNX 检测），
        详见 :doc:`pdf_flow`。

        Args:
            filepath: PDF 文件路径。
            filename: 是否优先从文件名中提取 ISBN。
            pdf_config: 可选的 PDF 页码配置覆盖，为 ``None`` 时从全局 ``settings.pdf`` 读取。

        Returns:
            :class:`~isbnx.models.ExtractResult` 包含 ISBN 提取结果。
        """
        require_suffix(filepath, (".pdf",), "PDF")
        if filename:
            info = extract_from_filename(filepath, strict=self.config.strict)
            if info:
                return ExtractResult(
                    bookinfo=info,
                    meta=Meta(source=str(filepath), source_type="pdf"),
                    elapsed=0.0,
                    from_filename=True,
                    strict=self.config.strict,
                )
        from isbnx.pdf import PdfExtractor

        return PdfExtractor.extract(filepath, detector=self.detector, pdf_config=pdf_config, config=self.config)

    # ── EPUB ──

    def from_epub(
        self,
        filepath: str | Path,
        *,
        filename: bool = False,
    ) -> ExtractResult:
        """从 EPUB 文件中提取 ISBN。

        优先解析 OPF 元数据中的 ``<dc:identifier>`` / ``<dc:isbn>``，
        未命中时扫描 XHTML 文件全文搜索 ISBN。

        Args:
            filepath: EPUB 文件路径。
            filename: 是否优先从文件名中提取 ISBN。

        Returns:
            :class:`~isbnx.models.ExtractResult` 包含 ISBN 提取结果。
        """
        require_suffix(filepath, (".epub",), "EPUB")
        if filename:
            info = extract_from_filename(filepath, strict=self.config.strict)
            if info:
                return ExtractResult(
                    bookinfo=info,
                    meta=Meta(source=str(filepath), source_type="epub"),
                    elapsed=0.0,
                    from_filename=True,
                    strict=self.config.strict,
                )
        from isbnx.epub import EpubExtractor

        return EpubExtractor.extract(filepath, detector=self.detector, config=self.config)

    # ── MOBI ──

    def from_mobi(
        self,
        filepath: str | Path,
        *,
        filename: bool = False,
    ) -> ExtractResult:
        """从 MOBI 文件中提取 ISBN。

        只扫描 MOBI 内部的文本和元数据，命中有效 ISBN 即返回。

        Args:
            filepath: MOBI 文件路径。
            filename: 是否优先从文件名中提取 ISBN。

        Returns:
            :class:`~isbnx.models.ExtractResult` 包含 ISBN 提取结果。
        """
        require_suffix(filepath, (".mobi",), "MOBI")
        if filename:
            info = extract_from_filename(filepath, strict=self.config.strict)
            if info:
                return ExtractResult(
                    bookinfo=info,
                    meta=Meta(source=str(filepath), source_type="mobi"),
                    elapsed=0.0,
                    from_filename=True,
                    strict=self.config.strict,
                )
        from isbnx.mobi import MobiExtractor

        return MobiExtractor.extract(filepath, config=self.config)

    # ── 压缩包（PDG / 其它） ──

    def from_archive(
        self,
        filepath: str | Path,
        *,
        filename: bool = False,
    ) -> ExtractResult:
        """从压缩包（zip/rar/7z/uvz）中提取 ISBN。

        数据来源优先级（按速度降序）：

        1. **文件名** — 从文件名中提取（``filename=True`` 时）
        2. **meta.xml** — XML 元数据，含 ``<ssid>`` / ``<isbn>``（最快，~10-20ms）
        3. **bookinfo.dat** — 超星 PDG 配置，含 ISBN / SSID（~5-10ms）
        4. **leg001.pdg** — 版权页图片，ONNX 检测 + OCR 提取（~200-500ms）
        5. **兜底 PDG** — 前 N 个 PDG 文件（由 ``archive_pdg_fallback_count`` 控制）

        meta.xml 和 bookinfo.dat 的结果会**合并**（前面的来源优先级更高）。
        合并后 ISBN 或 SSID 任一有效即返回，不继续走图片路径。

        Args:
            filepath: 压缩包文件路径。
            filename: 是否优先从文件名中提取 ISBN。

        Returns:
            :class:`~isbnx.models.ExtractResult` 包含 ISBN 提取结果。
        """
        require_suffix(filepath, (".zip", ".rar", ".uvz", ".7z"), "压缩包")
        if filename:
            info = extract_from_filename(filepath, strict=self.config.strict)
            if info:
                return ExtractResult(
                    bookinfo=info,
                    meta=Meta(source=str(filepath), source_type="archive"),
                    elapsed=0.0,
                    from_filename=True,
                    strict=self.config.strict,
                )
        from isbnx.archive import ArchiveExtractor

        return ArchiveExtractor.extract(filepath, detector=self.detector, config=self.config)


def extract(
    filepath: str | Path,
    config: Settings | None = None,
    page: int = 1,
    *,
    filename: bool = False,
) -> ExtractResult:
    """通用提取函数，根据后缀自动分发到最合适的提取入口。

    Args:
        filepath: 文件路径，支持图片 / PDF / EPUB / MOBI / 压缩包。
        config: 可选的配置对象，若不传则使用全局配置。
        page: 页码，仅对图片有效，PDF/EPUB/MOBI/压缩包忽略。
        filename: 是否优先从文件名中提取 ISBN。

    Returns:
        :class:`~isbnx.models.ExtractResult` 包含 ISBN 提取结果。
    """
    return ISBNX(config=config).extract(filepath, page=page, filename=filename)
