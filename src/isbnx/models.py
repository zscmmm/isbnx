"""ISBN 提取流程的数据模型。

定义检测、定位、OCR 识别及最终提取结果的所有数据结构。
所有模型均使用 Pydantic 进行校验与序列化。
"""

from __future__ import annotations

from functools import cached_property
from typing import Any, Literal

from mneia_isbn import ISBN  # type: ignore
from PIL import Image
from pydantic import BaseModel, ConfigDict, Field

from isbnx.config import settings

# ── ONNX 检测类别映射 ──
DETECT_CLASSES: dict[int, str] = {
    0: "alone",  # 独立的 ISBN 文字
    1: "cip",  # 出版社 CIP 页 ISBN
    2: "bar",  # 条形码 ISBN
}


class Detect(BaseModel):
    """ONNX 模型检测结果（单张图片的单个目标）。

    Attributes:
        box: 检测框坐标 (left, top, right, bottom)。
        image: 裁剪后的目标区域图片。
        score: 检测置信度 (0~1)。
        class_id: 检测到的目标类别 ID。
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    box: tuple[int, int, int, int]
    image: Image.Image
    score: float
    class_id: int = 0

    @property
    def class_name(self) -> str:
        """类别名称（alone / cip / bar）。"""
        return DETECT_CLASSES.get(self.class_id, f"unknown({self.class_id})")

    def __repr__(self) -> str:
        return f"Detect(box={self.box}, score={self.score:.3f}, class={self.class_name})"


class Locate(BaseModel):
    """ISBN 在源文件中的定位信息。

    对于多页文档（如 PDF），记录命中页码和定位方式；
    对于单张图片，方法固定为 ``"onnx"``。

    Attributes:
        page: 命中页码（1-indexed）。单张图片固定为 1。
            特殊值（仅压缩包/EPUB，无真实页码概念时使用）：

            - ``-1``: EPUB（无页概念）
            - ``-10``: 压缩包 leg001.pdg
            - ``-20``: 压缩包 bookinfo.dat
            - ``-21``: 压缩包 meta.xml

        method: 定位方式。

            - ``"onnx"``: 通过 ONNX 模型检测定位
            - ``"text"``: 通过 PDF 文本搜索定位
            - ``"bookmark"``: 通过书签（版权页/封底）定位
            - ``"meta"``: 从压缩包 meta.xml 元数据提取
            - ``"bookinfo"``: 从压缩包 bookinfo.dat 元数据提取
            - ``"leg001"``: 从压缩包 leg001.pdg 图片提取
            - ``"pdg"``: 从压缩包兜底 PDG 图片提取
            - ``"epub"``: 从 EPUB 元数据或 XHTML 内容提取

        detect: 最终成功 OCR 的 ONNX 检测结果，仅 ``onnx`` 方法时有值。
        candidates: 所有满足置信度阈值的 ONNX 候选框列表，仅 ``onnx`` 方法时有值。
    """

    page: int
    method: Literal["bookmark", "text", "onnx", "bookinfo", "leg001", "pdg", "epub", "meta"]

    extraction: Literal["text", "ocr", "opf", "opf+xhtml"] = "ocr"
    """数据提取方式。
    - ``text``: 从PDF 文本中提取（未经过ocr）
    - ``ocr``:  通过onnx检测+ocr识别提取
    - ``opf``:  从EPUB的opf元数据中提取
    - ``opf+xhtml``: 先从opf元数据中提取，如果没有再从xhtml内容中提取
    """

    detect: Detect | None = None
    candidates: list[Detect] = Field(default_factory=list)

    # ── 便捷属性 ──
    @property
    def image(self) -> Image.Image | None:
        """裁剪后的 ISBN 区域图片，仅 onnx 方法有值。"""
        return self.detect.image if self.detect else None

    @property
    def box(self) -> tuple[int, int, int, int] | None:
        """检测框坐标，仅 onnx 方法有值。"""
        return self.detect.box if self.detect else None

    @property
    def score(self) -> float | None:
        """检测置信度，仅 onnx 方法有值。"""
        return self.detect.score if self.detect else None

    def __repr__(self) -> str:
        parts = [f"page={self.page}", f"method={self.method}", f"extraction={self.extraction}"]
        if self.detect:
            d = self.detect
            parts.append(f"detect=Detect(box={d.box}, score={d.score:.3f}, class={d.class_name})")
        if self.candidates:
            scores = ", ".join(f"{c.score:.3f}" for c in self.candidates)
            parts.append(f"candidates={len(self.candidates)} [{scores}]")
        return f"Locate({', '.join(parts)})"


class Meta(BaseModel):
    """提取的文件级元信息。

    Attributes:
        source: 源文件路径。
        source_type: 源文件类型，``"pdf"`` / ``"image"`` / ``"archive"`` / ``"epub"`` / ``"mobi"``。
        pdf_type: PDF 子类型（``"text_based"`` / ``"scanned"`` 等）。
        encoding: bookinfo.dat 编码（``"gb18030"`` / ``"utf-8"``）。
    """

    source: str
    source_type: Literal["pdf", "image", "archive", "epub", "mobi"]
    pdf_type: str | None = None
    encoding: str | None = None

    def __repr__(self) -> str:
        parts = [f"source={self.source!r}", f"type={self.source_type!r}"]
        if self.pdf_type:
            parts.append(f"pdf_type={self.pdf_type!r}")
        if self.encoding:
            parts.append(f"encoding={self.encoding!r}")
        return f"Meta({', '.join(parts)})"


class OCRResult(BaseModel):
    """标准化 OCR 识别结果。

    Attributes:
        lines: OCR 识别的文本行列表（已清洗去空）。
        rawocr: OCR 引擎的原始输出（调试用）。
    """

    lines: list[str] = Field(default_factory=list)
    rawocr: Any = None

    @property
    def text(self) -> str:
        """全部文本行，用换行符拼接。"""
        return "\n".join(self.lines)

    def __repr__(self) -> str:
        return f"OCRResult(\n  lines={self.lines},\n  rawocr={self.rawocr}\n)"


class BookInfo(BaseModel):
    """书籍信息，仅关注 ISBN 和 SSID（压缩包特有）。

    Attributes:
        isbn: 书籍的 ISBN 号。
        ssid: 压缩包 bookinfo.dat 中的 SS 号（仅压缩包有）。
    """

    isbn: str | None = None
    ssid: str | None = None

    @cached_property
    def _isbn(self) -> ISBN | None:
        """缓存的 ISBN 对象，避免重复解析。"""
        if not self.isbn:
            return None
        return ISBN(self.isbn)

    @property
    def isbn_valid(self) -> bool:
        """校验 ISBN 是否有效。"""
        obj = self._isbn
        return obj.is_valid if obj else False

    @property
    def isbn13(self) -> str | None:
        """返回 ISBN-13 格式（如 ``9787123456789``）。"""
        obj = self._isbn
        return str(obj.as_isbn13) if (obj and obj.is_valid) else None

    @property
    def isbn10(self) -> str | None:
        """返回 ISBN-10 格式（如 ``712345678X``）。"""
        obj = self._isbn
        return str(obj.as_isbn10) if (obj and obj.is_valid) else None

    def is_valid(self, strict: int | None = None) -> bool:
        """校验是否有效。

        Args:
            strict: 严格等级（值越小越严格）。
                - ``1``: ISBN 和 SSID 都必须存在，且 ISBN 校验通过。
                - ``2``: ISBN 必须存在且校验通过。
                - ``3``: ISBN 校验通过 或 SSID 存在。
                ``None`` 时从 ``settings.strict`` 读取。
        """
        if strict is None:
            strict = settings.strict
        if strict <= 1:
            return bool(self.isbn_valid and self.ssid)
        if strict <= 2:
            return self.isbn_valid
        return bool(self.isbn_valid or self.ssid)


class ExtractResult(BaseModel):
    """一个文件的完整提取结果。

    所有字段铺在顶层，无论来源是图片 / PDF / 压缩包 / EPUB，
    调用方均可通过一致的接口访问。
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    # ── 提取结果 ──
    bookinfo: BookInfo = Field(default_factory=BookInfo)

    # ── 文件信息 ──
    meta: Meta

    # ── 定位与检测 ──
    locate: Locate | None = None

    # ── OCR 识别 ──
    ocr: OCRResult | None = None

    # ── 其他信息 ──
    elapsed: float | None = None
    error: str | None = None

    @property
    def success(self) -> bool:
        """提取是否成功（根据 strict 等级校验）。"""
        return self.bookinfo.is_valid()

    def __repr__(self) -> str:
        b = self.bookinfo
        lines = ["ExtractResult("]

        # ── success ──
        lines.append(f"  success={self.success}")

        # ── elapsed / error ──
        if self.elapsed is not None:
            lines.append(f"  elapsed={int(self.elapsed * 1000)}ms")
        if self.error:
            lines.append(f"  error={self.error!r}")

        # ── meta ──
        lines.append("  meta=Meta(")
        lines.append(f"    source={self.meta.source!r}")
        lines.append(f"    type={self.meta.source_type!r}")
        if self.meta.pdf_type:
            lines.append(f"    pdf_type={self.meta.pdf_type!r}")
        if self.meta.encoding:
            lines.append(f"    encoding={self.meta.encoding!r}")
        lines.append("  )")

        # ── bookinfo ──
        if b.isbn or b.ssid:
            lines.append("  bookinfo=BookInfo(")
            if b.isbn:
                lines.append(f"    isbn={b.isbn!r}")
                lines.append(f"    isbn_valid={b.isbn_valid}")
                if b.isbn_valid:
                    lines.append(f"    isbn13={b.isbn13!r}")
                    lines.append(f"    isbn10={b.isbn10!r}")
            if b.ssid:
                lines.append(f"    ssid={b.ssid!r}")
            lines.append("  )")
        else:
            lines.append("  bookinfo=BookInfo()  (isbn=None)")

        # ── locate / ocr（仅非 EPUB）──
        is_epub = self.meta.source_type == "epub"
        if not is_epub:
            if self.locate:
                loc = self.locate
                lines.append("  locate=Locate(")
                lines.append(f"    method={loc.method}")
                lines.append(f"    page={loc.page}")
                lines.append(f"    extraction={loc.extraction}")
                if loc.detect:
                    det = loc.detect
                    lines.append("    detect=Detect(")
                    lines.append(f"      box={det.box}")
                    lines.append(f"      score={det.score:.3f}")
                    lines.append(f"      class_id={det.class_id}")
                    lines.append(f"      class_name={det.class_name}")
                    lines.append("    )")
                if loc.candidates:
                    scores = ", ".join(f"{c.score:.3f}" for c in loc.candidates)
                    lines.append(f"    candidates={len(loc.candidates)}  scores=[{scores}]")
                lines.append("  )")
            else:
                lines.append("  locate=None")

            if self.ocr and self.ocr.lines:
                n = len(self.ocr.lines)
                lines.append(f"  ocr=OCRResult(lines={n}):")
                if n <= 15:
                    for j, line in enumerate(self.ocr.lines, 1):
                        lines.append(f"    [{j}] {line}")
                else:
                    for j, line in enumerate(self.ocr.lines[:10], 1):
                        lines.append(f"    [{j}] {line}")
                    lines.append(f"    ... ({n - 10} more)")
            elif self.ocr:
                lines.append("  ocr=OCRResult(lines=0)")
            else:
                lines.append("  ocr=None")

        lines.append(")")
        return "\n".join(lines)

    def __str__(self) -> str:
        return self.__repr__()
