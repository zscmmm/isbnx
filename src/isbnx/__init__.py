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
]


def main() -> None:
    """命令行入口：isbnx &lt;文件路径&gt;，提取 ISBN 并输出结果。"""
    import argparse

    parser = argparse.ArgumentParser(description="从文件提取 ISBN")
    parser.add_argument("path", help="文件路径（图片/PDF/EPUB/MOBI/压缩包）")
    parser.add_argument("--json", action="store_true", help="以 JSON 格式输出")
    parser.add_argument("--strict", type=int, default=3, help="严格等级 1/2/3（默认 3）")
    args = parser.parse_args()

    from isbnx.config import configure

    configure(strict=args.strict)
    result = extract(args.path)

    if args.json:
        print(result.to_json())
    else:
        print(result)
