"""PDF ISBN 提取模块。

流程:
  1. pdf-inspector 判断 PDF 类型（text_based / scanned）
  2. 书签检测 → 候选页生成（书签页优先级最高）
  3. text_based: 直接提取文本 → 搜索 ISBN → 命中则返回
  4. scanned / text 失败: 渲染页面为图片 → Detector.process() 检测
"""

from __future__ import annotations

import os
import re
import time
from pathlib import Path

import fitz  # PyMuPDF

# ── 全局抑制 MuPDF 错误输出 ──
# JM_mupdf_error 通过 message() → print(..., file=_g_out_message, flush=1) 输出，
# 而 _g_out_message 默认指向 sys.stdout，不受 stderr 重定向影响。
# 此处将 _g_out_message 置空，并关闭错误/警告显示开关。
import pymupdf as _pymupdf
from PIL import Image

from isbnx.config import PDFConfig, settings
from isbnx.detector import get_detector
from isbnx.models import BookInfo, ExtractResult, Locate, Meta, OCRResult
from isbnx.pdf_type import detect_pdf_type, detect_pdf_type2  # noqa: F401
from isbnx.utils.filename import extract_from_filename
from isbnx.utils.isbn_utils import extract_isbn

_pymupdf._g_out_message = open(os.devnull, "w", encoding="utf-8")
_pymupdf.JM_mupdf_show_errors = 0
_pymupdf.JM_mupdf_show_warnings = 0

# ── 书签关键词 ──
# (pattern, priority)  数字越小优先级越高
_BOOKMARK_RULES: list[tuple[re.Pattern, int]] = [
    (re.compile(r"版\s*权"), 0),  # "版权"，优先级最高
    (re.compile(r"封\s*底"), 1),  # "封底"，优先级次之
]


def _open_pdf(pdf_path: str | Path) -> tuple[fitz.Document | None, str | None]:
    """打开 PDF 文件，自动处理密码/加密。

    Returns:
        (doc, error) — 成功时 ``doc`` 有效、``error`` 为 None；
        失败时 ``doc`` 为 None、``error`` 为失败原因。
    """
    pdf_path = Path(pdf_path)
    if not pdf_path.exists():
        return None, "PDF 文件不存在"
    try:
        doc = fitz.open(str(pdf_path))
    except fitz.FileNotFoundError:
        return None, "PDF 文件不存在"
    except Exception:
        return None, "PDF 文件格式错误或损坏"
    if doc.needs_pass:
        doc.close()
        return None, "PDF 文件有密码保护"
    if doc.page_count == 0:
        doc.close()
        return None, "PDF 文件为空（0 页）"
    return doc, None


def _check_bookmarks(doc: fitz.Document) -> list[int]:
    """从书签中查找版权/封底相关页，按优先级返回列表。

    Returns:
        按优先级排序的页码列表（1-indexed），空列表表示未找到。
    """
    toc = doc.get_toc()
    if not toc:
        return []

    matched: list[tuple[int, int]] = []  # (page, priority)
    seen_pages: set[int] = set()
    for _level, title, page in toc:
        if page in seen_pages:
            continue
        for pattern, priority in _BOOKMARK_RULES:
            if pattern.search(title):
                matched.append((page, priority))
                seen_pages.add(page)
                break

    # 按优先级排序
    matched.sort(key=lambda x: x[1])
    return [page for page, _ in matched]


def _get_candidate_pages(
    page_count: int,
    *,
    pdf_config: PDFConfig | None = None,
) -> list[int]:
    """生成候选页码列表（1-indexed）。

    Args:
        page_count: PDF 总页数。
        pdf_config: 可选的 PDF 页码配置覆盖，为 ``None`` 时从全局 ``settings.pdf`` 读取。
    """
    cfg = pdf_config or settings.pdf
    front_start = max(1, cfg.front_start)
    front_end = min(page_count, cfg.front_end)
    back_start = max(1, page_count - cfg.back_start + 1)
    back_end = max(1, page_count - cfg.back_end + 1)

    front = list(range(front_start, front_end + 1))
    back = list(range(back_start, back_end + 1))

    seen: set[int] = set()
    result: list[int] = []
    for p in front + back:
        if p not in seen:
            seen.add(p)
            result.append(p)
    return result


# ISBN 关键词匹配（OCR 常见误识处理）
_ISBN_KEYWORD_PDF = re.compile(r"[1Il]\s*[S5]\s*[8B]\s*N", re.IGNORECASE)


def _search_isbn_in_text(text_lines: list[str]) -> str | None:
    """在文本行中搜索 ISBN，并验证行上下文不是 hash/水印噪音。

    对于 text_based PDF，文本中可能包含 hash/水印字符串（如 Anna's Archive
    嵌入 ``7RysGA...0c03l78...35+6...``），其中的散落或连续数字如果恰好
    通过 ISBN 校验和，会产生假阳性。此函数通过行上下文判断过滤。
    """
    for line in text_lines:
        isbn = extract_isbn(line)
        if isbn:
            # 行中有 "ISBN" 标记 → 直接信任
            if _ISBN_KEYWORD_PDF.search(line):
                return isbn
            # 行中有中文 → 信任（出版信息上下文）
            if re.search(r"[\u4e00-\u9fff]", line):
                return isbn
            # 行中有字母且 ISBN 不是行中唯一内容 → 可能是 hash/水印噪音
            # 例如 "abc9137011219def" 中 9137011219 嵌入在字母中
            if re.search(r"[A-Za-z]", line):
                # 检查 ISBN（纯数字）是否出现在行首附近
                # 检查 ISBN 前是否有字母前缀 → 说明数字嵌入在随机文本中
                cleaned_isbn = isbn.lstrip("0")
                pos = line.replace("-", "").replace(" ", "").find(cleaned_isbn)
                if pos > 2:
                    continue  # ISBN 前有字符 → 嵌入在文本中间，不可信
            return isbn
    return None


def _extract_text_from_page(doc: fitz.Document, page_num: int) -> list[str]:
    """从 PDF 页面提取文本行。"""
    try:
        page = doc[page_num - 1]
        text = page.get_text()  # type: ignore[arg-type]
        if not isinstance(text, str):
            text = str(text)
        return [line.strip() for line in text.split("\n") if line.strip()]
    except (IndexError, RuntimeError, ValueError, AttributeError):
        return []


def _render_page_to_image(
    doc: fitz.Document,
    page_num: int,
    zoom: float = 2.0,
    *,
    min_short_side: int = 600,
    max_short_side: int = 2000,
) -> Image.Image:
    """将 PDF 单页渲染为 PIL Image。

    根据页面物理尺寸自动调整 zoom 倍数，确保渲染结果既不过小也不过大的。

    * 页面物理尺寸**太小**（如 41x60 pt 的超小扫描件）：自动提高 zoom，
      使短边至少 ``min_short_side`` 像素，避免 ONNX 输入过小无法检测。
    * 页面物理尺寸**太大**（如大幅面扫描件）：自动降低 zoom，
      使短边不超过 ``max_short_side`` 像素，避免渲染超大图像浪费资源。

    Args:
        doc: PyMuPDF Document。
        page_num: 页码（1-indexed）。
        zoom: 基础缩放倍数（默认 2.0），在页面尺寸适中时使用。
        min_short_side: 渲染后图像短边的最小像素数（默认 600）。
        max_short_side: 渲染后图像短边的最大像素数（默认 2000）。
    """
    page = doc[page_num - 1]
    rect = page.rect
    short_side = min(rect.width, rect.height)
    # 如果页面物理尺寸太小，自动提高 zoom 以达到最小短边
    min_zoom = min_short_side / short_side if short_side > 0 else zoom
    # 如果页面物理尺寸太大，自动降低 zoom 以免超过最大短边
    max_zoom = max_short_side / short_side if short_side > 0 else zoom
    final_zoom = max(min_zoom, min(max_zoom, zoom))
    mat = fitz.Matrix(final_zoom, final_zoom)
    pix = page.get_pixmap(matrix=mat)
    return Image.frombytes("RGB", (pix.width, pix.height), pix.samples)


class PdfExtractor:
    """PDF ISBN 提取器。"""

    @classmethod
    def export_pages(
        cls,
        pdf_path: str | Path,
        out_dir: str | Path | None = None,
        *,
        dpi: float = 300,
        target_size: tuple[int, int] | None = None,
        fmt: str = "png",
        exist_ok: bool = True,
    ) -> Path:
        """将 PDF 所有页导出为图片。

        导出的图片统一放在 ``{pdf_stem}/`` 目录下，文件名格式为 ``{页码:04d}.png``。
        例如 ``AAA.pdf`` → ``AAA/0001.png``、``AAA/0002.png``……

        Args:
            pdf_path: PDF 文件路径。
            out_dir: 输出目录。默认取 PDF 同名目录（如 ``AAA.pdf`` → ``AAA/``）。
            dpi: 输出 DPI，所有页统一分辨率（默认 ``300``）。
                传 ``None`` 则用 PDF 原始 72 DPI。传 ``150`` 则 150 DPI。
            target_size: 统一输出尺寸 ``(width, height)``（像素）。指定后
                所有页会 resize 到此大小。与 ``dpi`` 互斥。
            fmt: 输出格式（默认 ``"png"``，支持 ``"jpg"``、``"webp"`` 等）。
            exist_ok: 若输出目录已存在是否继续（默认 ``True``）。

        Returns:
            输出目录的 ``Path``。

        Raises:
            FileNotFoundError: PDF 文件不存在。
            ValueError: PDF 无法打开或有密码保护。
            ValueError: ``dpi`` 和 ``target_size`` 同时指定。

        Example::

            # 导出为 300 DPI 图片（默认）
            PdfExtractor.export_pages("book.pdf")

            # 导出为原始 72 DPI
            PdfExtractor.export_pages("book.pdf", dpi=None)

            # 导出为固定尺寸
            PdfExtractor.export_pages("book.pdf", target_size=(800, 1200))
        """
        if dpi is not None and target_size is not None:
            raise ValueError("dpi 和 target_size 不能同时指定")

        pdf_path = Path(pdf_path)
        if not pdf_path.exists():
            raise FileNotFoundError(f"PDF 文件不存在: {pdf_path}")

        doc, open_error = _open_pdf(pdf_path)
        if doc is None:
            raise ValueError(open_error or "无法打开 PDF 文件")

        out_dir = Path(out_dir or pdf_path.parent / pdf_path.stem)
        out_dir.mkdir(parents=True, exist_ok=exist_ok)

        digit = len(str(doc.page_count))
        try:
            for page_num in range(1, doc.page_count + 1):
                if dpi is not None:
                    # 固定 DPI：zoom = dpi / 72（PDF 默认 72 DPI）
                    zoom = dpi / 72.0
                    mat = fitz.Matrix(zoom, zoom)
                    pix = doc[page_num - 1].get_pixmap(matrix=mat)
                    img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
                else:
                    # 原始分辨率（zoom=1.0，不作自动缩放）
                    img = _render_page_to_image(
                        doc,
                        page_num,
                        zoom=1.0,
                        min_short_side=1,
                        max_short_side=100000,
                    )

                if target_size is not None:
                    img = img.resize(target_size, Image.Resampling.LANCZOS)

                filename = f"{page_num:0{digit}d}.{fmt.lstrip('.')}"
                img.save(str(out_dir / filename))
        finally:
            doc.close()

        return out_dir

    @classmethod
    def extract(
        cls,
        pdf_path: str | Path,
        detector=None,
        *,
        filename: bool = False,
        pdf_config: PDFConfig | None = None,
    ) -> ExtractResult:
        """从 PDF 中提取 ISBN。

        Args:
            pdf_path: PDF 文件路径.
            filename: 是否优先从文件名中提取 ISBN。
            pdf_config: 可选的 PDF 页码配置覆盖，为 ``None`` 时从全局 ``settings.pdf`` 读取。

        Returns:
            ExtractResult — 包含 ISBN、定位信息、耗时等。
        """

        t0 = time.perf_counter()
        pdf_path = Path(pdf_path)

        if filename:
            info = extract_from_filename(pdf_path)
            if info:
                return ExtractResult(
                    bookinfo=info,
                    meta=Meta(source=str(pdf_path), source_type="pdf"),
                    elapsed=0.0,
                )

        # ── 打开 PDF ──
        doc, open_error = _open_pdf(pdf_path)
        if doc is None:
            return ExtractResult(
                bookinfo=BookInfo(),
                meta=Meta(source=str(pdf_path), source_type="pdf"),
                error=open_error or "无法打开 PDF 文件",
                elapsed=time.perf_counter() - t0,
            )

        # ── 1. 类型判断 ──
        try:
            import pdf_inspector  # type: ignore

            pdf_type = pdf_inspector.detect_pdf(str(pdf_path)).pdf_type
        except Exception:
            pdf_type = detect_pdf_type(doc)

        try:
            page_count = doc.page_count

            # ── 2. 书签检测 + 候选页 ──
            bookmark_pages = _check_bookmarks(doc)  # list[int], 按优先级排序
            candidates = _get_candidate_pages(page_count, pdf_config=pdf_config)
            # 按优先级将书签页插入候选列表最前
            for page in reversed(bookmark_pages):
                if page in candidates:
                    candidates.remove(page)
                candidates.insert(0, page)

            if not candidates:
                return ExtractResult(
                    bookinfo=BookInfo(),
                    meta=Meta(source=str(pdf_path), source_type="pdf", pdf_type=pdf_type),
                    error="无候选页面",
                    elapsed=time.perf_counter() - t0,
                )

            # ── 3. text_based: 文本提取 ──
            if pdf_type == "text_based":
                for page_num in candidates:
                    lines = _extract_text_from_page(doc, page_num)
                    if not lines:
                        continue
                    isbn_str = _search_isbn_in_text(lines)
                    if isbn_str:
                        locate_method = "bookmark" if page_num in bookmark_pages else "text"
                        return ExtractResult(
                            bookinfo=BookInfo(isbn=isbn_str),
                            meta=Meta(source=str(pdf_path), source_type="pdf", pdf_type=pdf_type),
                            locate=Locate(page=page_num, method=locate_method, extraction="text"),
                            ocr=OCRResult(lines=lines),
                            elapsed=time.perf_counter() - t0,
                        )

            # ── 4. scanned / text 失败: 渲染 + ONNX 检测 ──
            best_result: ExtractResult | None = None
            best_score: float = 0.0

            for page_num in candidates:
                img = _render_page_to_image(doc, page_num)
                det = detector or get_detector()
                result = det.process(img, source=str(pdf_path), source_type="pdf")
                if result.success:
                    result.locate.page = page_num  # type: ignore[union-attr]
                    result.elapsed = time.perf_counter() - t0
                    return result

                score = result.locate.detect.score if result.locate and result.locate.detect else 0.0
                if score > best_score:
                    best_score = score
                    best_result = result
                    best_result.locate.page = page_num  # type: ignore[union-attr]

            # 所有候选页均失败，但保留检测信息
            if best_result is not None:
                best_result.elapsed = time.perf_counter() - t0
                return best_result

            return ExtractResult(
                bookinfo=BookInfo(),
                meta=Meta(source=str(pdf_path), source_type="pdf", pdf_type=pdf_type),
                error="未检测到 ISBN 区域",
                elapsed=time.perf_counter() - t0,
            )

        finally:
            doc.close()
