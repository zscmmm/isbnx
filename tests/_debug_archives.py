"""分析压缩包差异样本。"""

from pathlib import Path

from cipx import CIPX
from cipx.config import Settings as CSettings

from isbnx import ISBNX
from isbnx.config import Settings as ISettings

SAMPLES = Path(r"D:\mmm\data\archives")
FILES = [
    "文景  派系分合与晚清政治  1885-1898  以\u201c帝后党争\u201d为中心的探讨_15589818.zip",
    "文景  纷纭万端  近代中国的思想与社会_15502989.zip",
    "荆楚文库  米芾集_15270513.zip",
]

cipx = CIPX(config=CSettings(strict=6))
isbnx = ISBNX(config=ISettings(strict=2))

for name in FILES:
    path = SAMPLES / name
    print(f"\n{'=' * 60}")
    print(f"  {name}")
    print(f"{'=' * 60}")

    r1 = cipx.from_archive(path)
    print(f"  cipx:  isbn={r1.bookinfo.isbn!r}  success={r1.success}  error={r1.error}")
    if r1.locate:
        print(f"         locate: method={r1.locate.method}  page={r1.locate.page}")
    if r1.ocr and r1.ocr.lines:
        print(f"         ocr lines({len(r1.ocr.lines)}): {r1.ocr.lines[:5]}")

    r2 = isbnx.from_archive(path)
    print(f"  isbnx: isbn={r2.bookinfo.isbn!r}  success={r2.success}  error={r2.error}")
    if r2.locate:
        print(f"         locate: method={r2.locate.method}  page={r2.locate.page}")
    if r2.ocr and r2.ocr.lines:
        print(f"         ocr lines({len(r2.ocr.lines)}): {r2.ocr.lines[:5]}")
