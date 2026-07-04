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
    print("Hello from isbnx!")
