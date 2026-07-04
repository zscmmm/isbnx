"""pyzbar 条码识别引擎（直接读取条码，无需 OCR）。"""

from __future__ import annotations

from PIL import Image
from pyzbar.pyzbar import ZBarSymbol, decode


class ISBNXZbar:
    """条码读取器，直接从条码图片中解码 ISBN。

    主要针对 EAN-13（ISBN 标准条码格式），
    同时支持其他常见条码格式作为回退。
    """

    # 优先尝试的条码符号类型（EAN-13 是最常见的 ISBN 条码格式）
    _PREFERRED = [ZBarSymbol.EAN13]
    # 回退符号类型
    _FALLBACK = [
        ZBarSymbol.ISBN10,
        ZBarSymbol.ISBN13,
    ]

    def __init__(self, preferred_only: bool = True) -> None:
        """
        Args:
            preferred_only: 仅尝试 EAN-13，为 False 时尝试所有常见格式。
        """
        self._symbols = self._PREFERRED if preferred_only else self._FALLBACK

    def decode(self, image: Image.Image) -> str | None:
        """解码图片中的条码，返回第一条文本，无结果返回 None。"""
        results = decode(image, symbols=self._symbols)
        for r in results:
            text = r.data.decode("utf-8", errors="replace").strip()
            if text:
                return text
        return None
