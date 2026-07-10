from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from isbnx.batch import Batch, BatchResult
    from isbnx.config import Settings, configure, settings
    from isbnx.isbnx import ISBNX, extract
    from isbnx.models import Detect, ExtractResult, Locate, Meta, OCRResult

__all__ = [
    "Settings",
    "configure",
    "settings",
    "ISBNX",
    "extract",
    "Detect",
    "Locate",
    "Meta",
    "OCRResult",
    "ExtractResult",
    "Batch",
    "BatchResult",
]


def __getattr__(name: str):
    """懒加载子模块，避免 `import isbnx` 时触发 pydantic/onnxruntime 等重型导入。"""
    import importlib

    _lazy_map: dict[str, str] = {
        "Settings": "isbnx.config",
        "configure": "isbnx.config",
        "settings": "isbnx.config",
        "ISBNX": "isbnx.isbnx",
        "extract": "isbnx.isbnx",
        "Detect": "isbnx.models",
        "Locate": "isbnx.models",
        "Meta": "isbnx.models",
        "OCRResult": "isbnx.models",
        "ExtractResult": "isbnx.models",
        "Batch": "isbnx.batch",
        "BatchResult": "isbnx.batch",
    }
    if name in _lazy_map:
        mod = importlib.import_module(_lazy_map[name])
        return getattr(mod, name)
    msg = f"module {__name__!r} has no attribute {name!r}"
    raise AttributeError(msg)


def main() -> None:
    """命令行入口：isbnx &lt;文件路径&gt;，提取 ISBN 并输出结果。"""
    import argparse

    parser = argparse.ArgumentParser(description="从文件提取 ISBN")
    parser.add_argument("path", help="文件路径（图片/PDF/EPUB/MOBI/压缩包）")
    parser.add_argument("--json", action="store_true", help="以 JSON 格式输出")
    parser.add_argument("--strict", type=int, default=3, help="严格等级 1/2/3（默认 3）")
    parser.add_argument("--filename", action="store_true", help="优先从文件名中提取 ISBN")
    args = parser.parse_args()

    from isbnx.config import configure

    configure(strict=args.strict)
    result = extract(args.path, filename=args.filename)

    if args.json:
        print(result.to_json())
    else:
        print(result)
