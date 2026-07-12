"""ISBN 提取入口，提供统一的提取接口。"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import TYPE_CHECKING

from isbnx.utils.io import detect_file_kind

if TYPE_CHECKING:
    from isbnx.batch import BatchResult
    from isbnx.config import PDFConfig, Settings
    from isbnx.detector import Detector
    from isbnx.models import ExtractResult


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

        # 批量处理（复用当前实例的配置和引擎）
        result = ISBNX().batch("D:/books", "D:/done", "D:/fail")
    """

    def __init__(self, config: Settings | None = None) -> None:
        from threading import Lock

        from isbnx.config import settings

        self.config = config or settings
        self._apply_config()
        self._detector: Detector | None = None  # 懒加载，首次需要时再初始化
        self._detector_lock = Lock()

    @property
    def detector(self) -> Detector:
        """获取检测器（懒加载，首次访问时预热 ONNX + OCR）。"""
        from isbnx.detector import get_detector

        if self._detector is None:
            with self._detector_lock:
                if self._detector is None:
                    self._detector = get_detector()
        return self._detector

    def _apply_config(self) -> None:
        """将实例级配置同步到全局 settings 对象。"""
        from isbnx.config import configure, settings

        if self.config is not settings:
            configure(**self.config.model_dump())

    # ── 批量处理 ──

    def batch(
        self,
        source_dir: str | Path,
        success_dir: str | Path,
        failed_dir: str | Path,
        *,
        extensions: Iterable[str] | None = None,
        exclude_dirs: set[str] | None = None,
        max_workers: int | None = None,
        recursive: bool = True,
        rename_mode: int = 3,
        pdf_front_start: int | None = None,
        pdf_front_end: int | None = None,
        pdf_back_start: int | None = None,
        pdf_back_end: int | None = None,
        skip_isbn: bool = True,
        skip_ssid: bool = False,
        normalize_ext: bool = True,
        keep_name: bool = True,
        quiet: bool = True,
        show_progress: bool = False,
        keep_tree: bool = False,
        deduplicate: bool = False,
        dedup_read_size: int = 4096,
        max_name_len: int = 180,
        report_path: str | Path | None = None,
        dry_run: bool = False,
        shutdown_event: threading.Event | None = None,
        progress_callback: Callable[[int, int, str], None] | None = None,
        entries_callback: Callable[[str, str, float, str], None] | None = None,
        max_entries: int = 1000,
        remove_empty_dirs: bool = False,
    ) -> BatchResult:
        """创建批量处理器，复用当前实例的配置和引擎。

        Args:
            source_dir: 待扫描的源目录。
            success_dir: ISBN 提取成功的文件移动到此目录。
            failed_dir: ISBN 提取失败的文件移动到此目录。
            extensions: 要处理的文件后缀集合（如 ``{".pdf", ".epub"}``），
                必须为 ``SUPPORTED_EXTENSIONS`` 的子集，默认 ``None``=处理所有支持的类型。
            exclude_dirs: 要跳过的目录名集合，默认排除 ``.git``、``__pycache__`` 等。
            max_workers: 并行线程数，默认 ``os.cpu_count() - 1``。
            recursive: 是否递归扫描子目录，默认 ``True``。
            rename_mode: 重命名模式 —
                ``1``=末尾追加（旧标识不变），``2``=最前面追加（旧标识不变），
                ``3``=替换旧标识再末尾追加（默认），``4``=替换旧标识再最前面追加。
                模式 1/2 中文件名已有标识则不重复。
            pdf_front_start: PDF 前部搜索起始页码偏移（默认 2）。
            pdf_front_end: PDF 前部搜索结束页码偏移（默认 10）。
            pdf_back_start: PDF 后部搜索起始页码偏移（默认 5）。
            pdf_back_end: PDF 后部搜索结束页码偏移（默认 1）。
            skip_isbn: 文件名有 ISBN 时跳过内容提取，默认 ``True``。
            skip_ssid: 文件名有 SSID 时跳过内容提取，默认 ``False``。
            normalize_ext: 统一后缀为小写（``.PDF`` → ``.pdf``），默认 ``True``。
            keep_name: 保留原文件名书名部分（默认 ``True``），``False``=仅用 ISBN/SSID 命名。
            quiet: 安静模式，不逐文件打印日志，默认 ``False``。
            show_progress: 显示 tqdm 进度条，默认 ``True``。与 ``quiet`` 独立控制。
            keep_tree: 保留源目录的子目录结构，默认 ``False``。
            deduplicate: 对内容完全相同的文件去重，默认 ``False``。
                启用后会先按大小和头部指纹初筛，再用完整哈希确认。
            dedup_read_size: 去重读取文件头部的字节数，默认 ``4096``（4KB）。
                ``0``=跳过头部初筛，同尺寸文件直接做完整哈希确认。
            max_name_len: 文件名最大长度（含后缀），默认 ``180``。
            report_path: 可选，保存 CSV 报告到此路径。
            dry_run: 干运行（仅预览），不实际移动文件。
            shutdown_event:
                可选的 ``threading.Event`` 对象，用于从外部优雅终止批量处理。
                默认 ``None`` 表示不启用关闭机制。

                用法：创建一个 ``threading.Event``，在需要取消时调用 ``event.set()``。
                处理过程中会在文件名预检、线程池提交等关键节点检查此事件，
                触发后立即停止新任务的提交，已提交正在执行的任务等待完成
                （不强制中断线程），然后返回已处理的结果统计。

                典型场景 — GUI 取消按钮或服务关闭钩子::

                    import threading

                    cancel_event = threading.Event()


                    # 另开线程执行批量处理
                    def run():
                        result = ISBNX().batch("src", "ok", "fail", shutdown_event=cancel_event)


                    # 用户点击取消时
                    cancel_event.set()

            progress_callback:
                可选的进度回调函数，格式 ``Callable[[int, int, str], None]``。
                默认 ``None`` 表示不启用进度回调。

                签名 ``(processed: int, total: int, filename: str) -> None``。
                每处理完一个文件后调用，三个参数分别为：已处理文件数、总文件数、
                当前文件名。可用于驱动进度条控件。

                传值示例::

                    def on_progress(processed: int, total: int, name: str) -> None:
                        print(f"[{processed}/{total}] {name}")


                    ISBNX().batch("src", "ok", "fail", progress_callback=on_progress)

            entries_callback:
                可选的逐条结果回调函数，格式 ``Callable[[str, str, float, str], None]``。
                默认 ``None`` 表示不启用逐条回调。

                签名 ``(old_path: str, new_path: str, elapsed: float, outcome: str) -> None``。
                每处理完一个文件后调用，四个参数分别为：原路径、新路径、耗时（秒）、
                结果分类（如 ``"isbn_appended"`` / ``"failed"`` / …）。
                可用于实时同步处理结果到外部系统（如数据库、UI 列表）。

                传值示例::

                    def on_entry(old: str, new: str, elapsed: float, outcome: str) -> None:
                        print(f"{outcome}: {old} → {new} ({elapsed:.2f}s)")


                    ISBNX().batch("src", "ok", "fail", entries_callback=on_entry)

            max_entries:
                ``result.entries`` 列表的最大条目数，默认 ``1000``。
                设为 ``0`` 或负数表示不限制。
                当处理大量文件时，限制 entries 大小可避免返回数据过大。

            remove_empty_dirs:
                处理完成后是否删除源目录下的空目录，默认 ``False``。
                自底向上扫描，删除所有不含任何文件的空目录。
                仅 ``dry_run=False`` 时实际删除，干运行模式下仅打印日志。

        Returns:
            :class:`~isbnx.batch.BatchResult` 处理结果统计。
        """
        from isbnx.batch import Batch

        return Batch(
            source_dir,
            success_dir,
            failed_dir,
            extensions=extensions,
            exclude_dirs=exclude_dirs,
            max_workers=max_workers,
            recursive=recursive,
            engine=self,
            rename_mode=rename_mode,
            pdf_front_start=pdf_front_start,
            pdf_front_end=pdf_front_end,
            pdf_back_start=pdf_back_start,
            pdf_back_end=pdf_back_end,
            skip_isbn=skip_isbn,
            skip_ssid=skip_ssid,
            normalize_ext=normalize_ext,
            keep_name=keep_name,
            quiet=quiet,
            show_progress=show_progress,
            keep_tree=keep_tree,
            deduplicate=deduplicate,
            dedup_read_size=dedup_read_size,
            max_name_len=max_name_len,
            report_path=report_path,
            dry_run=dry_run,
            shutdown_event=shutdown_event,
            progress_callback=progress_callback,
            entries_callback=entries_callback,
            max_entries=max_entries,
            remove_empty_dirs=remove_empty_dirs,
        ).run()

    # ── 单张图片 ──

    def from_image(
        self,
        path: str | Path,
        page: int = 1,
        *,
        filename: bool = False,
    ) -> ExtractResult:
        """从单张图片文件中提取 ISBN。

        支持常见图片格式（PNG/JPG/WebP/BMP 等）以及加密或非加密的 PDG 文件。
        PDG 文件会自动尝试解密后提取。

        Args:
            path: 图片文件路径。
            page: 页码（预留参数，仅兼容单页图片）。
            filename: 是否优先从文件名中提取 ISBN。

        Returns:
            :class:`~isbnx.models.ExtractResult` 包含 ISBN 提取结果。
        """
        path = Path(path)

        if filename:
            from isbnx.utils.filename import extract_from_filename

            info = extract_from_filename(path)
            if info:
                from isbnx.models import BookInfo, ExtractResult, Meta

                return ExtractResult(
                    bookinfo=info,
                    meta=Meta(source=str(path), source_type="image"),
                    elapsed=0.0,
                    from_filename=True,
                )

        if page != 1:
            raise NotImplementedError("仅支持单页图片")

        if path.suffix.lower() == ".pdg":
            from isbnx.archive import _pdg_to_image

            data = path.read_bytes()
            image = _pdg_to_image(data)
            if image is None:
                from isbnx.models import BookInfo, ExtractResult, Meta

                return ExtractResult(
                    bookinfo=BookInfo(),
                    meta=Meta(source=str(path), source_type="image"),
                    error="PDG 文件解码失败",
                )
        else:
            from isbnx.utils.io import load_image

            image = load_image(path)

        return self.detector.process(image, source=str(path), source_type="image")

    def extract(
        self,
        path: str | Path,
        page: int = 1,
        *,
        filename: bool = False,
        pdf_config: PDFConfig | None = None,
    ) -> ExtractResult:
        """根据文件后缀自动选择对应的提取方法。

        Args:
            path: 文件路径。
            page: 页码，仅对图片有效。
            filename: 是否优先从文件名中提取 ISBN。
            pdf_config: 可选的 PDF 页码配置覆盖，为 ``None`` 时从全局 ``settings.pdf`` 读取。

        Returns:
            :class:`~isbnx.models.ExtractResult` 包含 ISBN 提取结果。
        """
        kind = detect_file_kind(path)
        if kind == "image":
            return self.from_image(path, page=page, filename=filename)
        if kind == "pdf":
            return self.from_pdf(path, filename=filename, pdf_config=pdf_config)
        if kind == "epub":
            return self.from_epub(path, filename=filename)
        if kind == "mobi":
            return self.from_mobi(path, filename=filename)
        return self.from_archive(path, filename=filename)

    # ── PDF ──

    def from_pdf(
        self,
        path: str | Path,
        *,
        filename: bool = False,
        pdf_config: PDFConfig | None = None,
    ) -> ExtractResult:
        """从 PDF 文件中提取 ISBN。

        支持文本型 PDF（文本搜索）和扫描件（渲染为图片后 ONNX 检测），
        详见 :doc:`pdf_flow`。

        Args:
            path: PDF 文件路径。
            filename: 是否优先从文件名中提取 ISBN。
            pdf_config: 可选的 PDF 页码配置覆盖，为 ``None`` 时从全局 ``settings.pdf`` 读取。

        Returns:
            :class:`~isbnx.models.ExtractResult` 包含 ISBN 提取结果。
        """
        from isbnx.utils.io import require_suffix

        require_suffix(path, (".pdf",), "PDF")
        if filename:
            from isbnx.utils.filename import extract_from_filename

            info = extract_from_filename(path)
            if info:
                from isbnx.models import ExtractResult, Meta

                return ExtractResult(
                    bookinfo=info,
                    meta=Meta(source=str(path), source_type="pdf"),
                    elapsed=0.0,
                    from_filename=True,
                )
        from isbnx.pdf import PdfExtractor

        return PdfExtractor.extract(path, detector=self.detector, pdf_config=pdf_config)

    # ── EPUB ──

    def from_epub(
        self,
        path: str | Path,
        *,
        filename: bool = False,
    ) -> ExtractResult:
        """从 EPUB 文件中提取 ISBN。

        优先解析 OPF 元数据中的 ``<dc:identifier>`` / ``<dc:isbn>``，
        未命中时扫描 XHTML 文件全文搜索 ISBN。

        Args:
            path: EPUB 文件路径。
            filename: 是否优先从文件名中提取 ISBN。

        Returns:
            :class:`~isbnx.models.ExtractResult` 包含 ISBN 提取结果。
        """
        from isbnx.utils.io import require_suffix

        require_suffix(path, (".epub",), "EPUB")
        if filename:
            from isbnx.utils.filename import extract_from_filename

            info = extract_from_filename(path)
            if info:
                from isbnx.models import ExtractResult, Meta

                return ExtractResult(
                    bookinfo=info,
                    meta=Meta(source=str(path), source_type="epub"),
                    elapsed=0.0,
                    from_filename=True,
                )
        from isbnx.epub import EpubExtractor

        return EpubExtractor.extract(path)

    # ── MOBI ──

    def from_mobi(
        self,
        path: str | Path,
        *,
        filename: bool = False,
    ) -> ExtractResult:
        """从 MOBI 文件中提取 ISBN。

        只扫描 MOBI 内部的文本和元数据，命中有效 ISBN 即返回。

        Args:
            path: MOBI 文件路径。
            filename: 是否优先从文件名中提取 ISBN。

        Returns:
            :class:`~isbnx.models.ExtractResult` 包含 ISBN 提取结果。
        """
        if filename:
            from isbnx.utils.filename import extract_from_filename

            info = extract_from_filename(path)
            if info:
                from isbnx.models import ExtractResult, Meta

                return ExtractResult(
                    bookinfo=info,
                    meta=Meta(source=str(path), source_type="mobi"),
                    elapsed=0.0,
                    from_filename=True,
                )
        from isbnx.mobi import MobiExtractor

        return MobiExtractor.extract(path)

    # ── 压缩包（PDG / 其它） ──

    def from_archive(
        self,
        path: str | Path,
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
            path: 压缩包文件路径。
            filename: 是否优先从文件名中提取 ISBN。

        Returns:
            :class:`~isbnx.models.ExtractResult` 包含 ISBN 提取结果。
        """
        from isbnx.utils.io import require_suffix

        require_suffix(path, (".zip", ".rar", ".uvz", ".7z"), "压缩包")
        if filename:
            from isbnx.utils.filename import extract_from_filename

            info = extract_from_filename(path)
            if info:
                from isbnx.models import ExtractResult, Meta

                return ExtractResult(
                    bookinfo=info,
                    meta=Meta(source=str(path), source_type="archive"),
                    elapsed=0.0,
                    from_filename=True,
                )
        from isbnx.archive import ArchiveExtractor

        return ArchiveExtractor.extract(path, detector=self.detector)


def extract(
    path: str | Path,
    config: Settings | None = None,
    page: int = 1,
    *,
    filename: bool = False,
) -> ExtractResult:
    """通用提取函数，根据后缀自动分发到最合适的提取入口。

    Args:
        path: 文件路径，支持图片 / PDF / EPUB / MOBI / 压缩包。
        config: 可选的配置对象，若不传则使用全局配置。
        page: 页码，仅对图片有效，PDF/EPUB/MOBI/压缩包忽略。
        filename: 是否优先从文件名中提取 ISBN。

    Returns:
        :class:`~isbnx.models.ExtractResult` 包含 ISBN 提取结果。
    """
    if filename:
        from isbnx.utils.filename import extract_from_filename

        info = extract_from_filename(path)
        if info:
            try:
                _kind = detect_file_kind(path)
            except ValueError:
                _kind = "pdf"
            from isbnx.models import ExtractResult, Meta

            return ExtractResult(
                bookinfo=info,
                meta=Meta(source=str(path), source_type=_kind),
                elapsed=0.0,
                from_filename=True,
            )
    from isbnx.isbnx import ISBNX

    return ISBNX(config=config).extract(path, page=page, filename=filename)
