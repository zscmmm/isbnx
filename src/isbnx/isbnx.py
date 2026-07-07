"""ISBN 提取入口，提供统一的提取接口。"""

from __future__ import annotations

from pathlib import Path

from isbnx.archive import ArchiveExtractor
from isbnx.config import Settings, configure, settings
from isbnx.detector import get_detector
from isbnx.epub import EpubExtractor
from isbnx.mobi import MobiExtractor
from isbnx.models import ExtractResult
from isbnx.pdf import PdfExtractor
from isbnx.utils.io import detect_file_kind, load_image, require_suffix


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
        self._apply_config()
        self._detector = get_detector()  # 预热 ONNX + OCR

    def _apply_config(self) -> None:
        """将实例级配置同步到全局 settings 对象。"""
        if self.config is not settings:
            configure(**self.config.model_dump())

    # ── 单张图片 ──

    def from_image(
        self,
        path: str | Path,
        page: int = 1,
    ) -> ExtractResult:
        """从单张图片文件中提取 ISBN。

        支持常见图片格式（PNG/JPG/WebP/BMP 等）以及加密或非加密的 PDG 文件。
        PDG 文件会自动尝试解密后提取。

        Args:
            path: 图片文件路径。
            page: 页码（预留参数，多页图片暂不支持）。

        Returns:
            :class:`~isbnx.models.ExtractResult` 包含 ISBN 提取结果。
        """
        if page != 1:
            raise NotImplementedError("多页图片暂不支持指定页码")

        path = Path(path)
        if path.suffix.lower() == ".pdg":
            from isbnx.archive import _pdg_to_image

            data = path.read_bytes()
            image = _pdg_to_image(data)
            if image is None:
                from isbnx.models import BookInfo, Meta

                return ExtractResult(
                    bookinfo=BookInfo(),
                    meta=Meta(source=str(path), source_type="image"),
                    error="PDG 文件解码失败",
                )
        else:
            image = load_image(path)

        return self._detector.process(image, source=str(path), source_type="image")

    def extract(
        self,
        path: str | Path,
        page: int = 1,
    ) -> ExtractResult:
        """根据文件后缀自动选择对应的提取方法。"""
        kind = detect_file_kind(path)
        if kind == "image":
            return self.from_image(path, page=page)
        if kind == "pdf":
            return self.from_pdf(path)
        if kind == "epub":
            return self.from_epub(path)
        if kind == "mobi":
            return self.from_mobi(path)
        return self.from_archive(path)

    # ── PDF ──

    def from_pdf(
        self,
        path: str | Path,
    ) -> ExtractResult:
        """从 PDF 文件中提取 ISBN。

        支持文本型 PDF（文本搜索）和扫描件（渲染为图片后 ONNX 检测），
        详见 :doc:`pdf_flow`。

        Args:
            path: PDF 文件路径。

        Returns:
            :class:`~isbnx.models.ExtractResult` 包含 ISBN 提取结果。
        """
        require_suffix(path, (".pdf",), "PDF")
        return PdfExtractor.extract(path, detector=self._detector)

    # ── EPUB ──

    def from_epub(
        self,
        path: str | Path,
    ) -> ExtractResult:
        """从 EPUB 文件中提取 ISBN。

        优先解析 OPF 元数据中的 ``<dc:identifier>`` / ``<dc:isbn>``，
        未命中时扫描 XHTML 文件全文搜索 ISBN。

        Args:
            path: EPUB 文件路径。

        Returns:
            :class:`~isbnx.models.ExtractResult` 包含 ISBN 提取结果。
        """
        require_suffix(path, (".epub",), "EPUB")
        return EpubExtractor.extract(path)

    # ── MOBI ──

    def from_mobi(
        self,
        path: str | Path,
    ) -> ExtractResult:
        """从 MOBI 文件中提取 ISBN。

        只扫描 MOBI 内部的文本和元数据，命中有效 ISBN 即返回。

        Args:
            path: MOBI 文件路径。

        Returns:
            :class:`~isbnx.models.ExtractResult` 包含 ISBN 提取结果。
        """
        return MobiExtractor.extract(path)

    # ── 压缩包（PDG / 其它） ──

    def from_archive(
        self,
        path: str | Path,
    ) -> ExtractResult:
        """从压缩包（zip/uvz）中提取 ISBN。

        数据来源优先级（按速度降序）：

        1. **meta.xml** — XML 元数据，含 ``<ssid>`` / ``<isbn>``（最快，~10-20ms）
        2. **bookinfo.dat** — 超星 PDG 配置，含 ISBN / SSID（~5-10ms）
        3. **leg001.pdg** — 版权页图片，ONNX 检测 + OCR 提取（~200-500ms）
        4. **兜底 PDG** — 前 N 个 PDG 文件（由 ``archive_pdg_fallback_count`` 控制）

        meta.xml 和 bookinfo.dat 的结果会**合并**（前面的来源优先级更高）。
        合并后 ISBN 或 SSID 任一有效即返回，不继续走图片路径。

        Args:
            path: 压缩包文件路径。

        Returns:
            :class:`~isbnx.models.ExtractResult` 包含 ISBN 提取结果。
        """
        require_suffix(path, (".zip", ".rar", ".uvz"), "压缩包")
        return ArchiveExtractor.extract(path, detector=self._detector)


def extract(
    path: str | Path,
    config: Settings | None = None,
    page: int = 1,
) -> ExtractResult:
    """通用提取函数，根据后缀自动分发到最合适的提取入口。

    Args:
        path: 文件路径，支持图片 / PDF / EPUB / MOBI / 压缩包。
        config: 可选的配置对象，若不传则使用全局配置。
        page: 页码，仅对图片有效，PDF/EPUB/MOBI/压缩包忽略。

    Returns:
        :class:`~isbnx.models.ExtractResult` 包含 ISBN 提取结果。
    """
    return ISBNX(config=config).extract(path, page=page)
