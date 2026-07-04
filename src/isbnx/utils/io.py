from __future__ import annotations

from io import BytesIO
from pathlib import Path
from typing import Literal

import numpy as np
from cv2.typing import MatLike
from PIL import Image, ImageOps

ExtractKind = Literal["image", "pdf", "epub", "mobi", "archive"]

_IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".tif", ".tiff")
_PDF_SUFFIXES = (".pdf",)
_EPUB_SUFFIXES = (".epub",)
_MOBI_SUFFIXES = (".mobi",)
_ARCHIVE_SUFFIXES = (".zip", ".rar", ".uvz")


def require_suffix(path: str | Path, suffixes: tuple[str, ...], kind: str) -> Path:
    """快速校验文件后缀，避免把明显不对的输入交给下层解析。

    Args:
        path: 文件路径。
        suffixes: 允许的后缀集合，如 ``(".pdf",)``。
        kind: 描述文件类型的字符串，用于错误提示，如 ``"PDF"``。

    Returns:
        解析后的 ``Path`` 对象。

    Raises:
        ValueError: 后缀不匹配时抛出。
    """
    file_path = Path(path)
    ext = file_path.suffix.lower()
    if ext not in suffixes:
        allowed = ", ".join(suffixes)
        raise ValueError(f"不支持的{kind}格式: {ext or '无后缀'}（支持 {allowed}）")
    return file_path


def detect_file_kind(path: str | Path) -> ExtractKind:
    """根据后缀快速判断该文件适合走哪条提取路径。"""
    ext = Path(path).suffix.lower()
    if ext in _IMAGE_SUFFIXES:
        return "image"
    if ext in _PDF_SUFFIXES:
        return "pdf"
    if ext in _EPUB_SUFFIXES:
        return "epub"
    if ext in _MOBI_SUFFIXES:
        return "mobi"
    if ext in _ARCHIVE_SUFFIXES:
        return "archive"
    raise ValueError(f"不支持的文件格式: {ext or '无后缀'}（支持图片/pdf/epub/mobi/zip/rar/uvz）")


def load_image(img: str | Image.Image | MatLike | Path | bytes) -> Image.Image:
    """统一加载图片为 RGB PIL Image，支持路径 / bytes / ndarray / PIL。"""
    if isinstance(img, Image.Image):
        return ImageOps.exif_transpose(img).convert("RGB")
    if isinstance(img, (str, Path)):
        with Image.open(img) as image:
            return ImageOps.exif_transpose(image).convert("RGB")
    if isinstance(img, bytes):
        with Image.open(BytesIO(img)) as image:
            return ImageOps.exif_transpose(image).convert("RGB")

    array = np.asarray(img)
    if array.ndim == 2:
        return Image.fromarray(array).convert("RGB")
    if array.ndim == 3 and array.shape[2] == 4:
        return Image.fromarray(array[:, :, [2, 1, 0, 3]], "RGBA").convert("RGB")
    if array.ndim == 3 and array.shape[2] == 3:
        return Image.fromarray(array[:, :, ::-1], "RGB").convert("RGB")
    raise ValueError(f"Unsupported image array shape: {array.shape}")
