"""OCR 引擎模块（懒加载，避免导入时即加载重型 OCR 模型）。"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from isbnx.ocr.isbnx_pyzbar import ISBNXZbar
    from isbnx.ocr.isbnx_rapiocr import ISBNXRapidOCR

__all__ = [
    "ISBNXRapidOCR",
    "ISBNXZbar",
]


def __getattr__(name: str):
    if name == "ISBNXRapidOCR":
        from isbnx.ocr.isbnx_rapiocr import ISBNXRapidOCR

        return ISBNXRapidOCR
    if name == "ISBNXZbar":
        from isbnx.ocr.isbnx_pyzbar import ISBNXZbar

        return ISBNXZbar
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
