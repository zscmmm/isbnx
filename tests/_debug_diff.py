"""对比分析差异样本的检测细节。"""

from pathlib import Path

from cipx import CIPX
from cipx.config import Settings as CSettings

from isbnx import ISBNX
from isbnx.config import Settings as ISettings

SAMPLES = Path(r"D:\mmm\data\images")
FILES = [
    "leg001_0058.png",  # cipx 未检测到, isbnx 找到
    "leg001_0347.png",  # ISBN 不同
    "leg001_0525.png",  # cipx 找到, isbnx 未找到
    "leg001_0915.png",  # cipx 未检测到, isbnx 找到
    "raw_0241.png",  # cipx isbn 无效但 valid=True
]

cipx = CIPX(config=CSettings(strict=6))
isbnx = ISBNX(config=ISettings(strict=2))

for name in FILES:
    path = SAMPLES / name
    print(f"\n{'=' * 60}")
    print(f"  {name}")
    print(f"{'=' * 60}")

    r1 = cipx.from_image(path)
    print(f"  cipx:  isbn={r1.bookinfo.isbn!r}  success={r1.success}  error={r1.error}")
    if r1.locate:
        print(f"         locate: method={r1.locate.method}  page={r1.locate.page}")
        if r1.locate.detect:
            d = r1.locate.detect
            print(f"         detect: box={d.box}  score={d.score:.3f}  class={d.class_name}")
        if r1.ocr:
            print(f"         ocr: {r1.ocr}")

    r2 = isbnx.from_image(path)
    print(f"  isbnx: isbn={r2.bookinfo.isbn!r}  success={r2.success}  error={r2.error}")
    if r2.locate:
        print(f"         locate: method={r2.locate.method}  page={r2.locate.page}")
        if r2.locate.detect:
            d = r2.locate.detect
            print(f"         detect: box={d.box}  score={d.score:.3f}  class={d.class_name}")
        if r2.ocr:
            print(f"         ocr: {r2.ocr}")
