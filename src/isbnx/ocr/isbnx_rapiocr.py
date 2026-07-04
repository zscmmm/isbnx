"""RapidOCR ONNX 运行时 OCR 引擎封装。"""

from __future__ import annotations

import numpy as np
from PIL import Image

from isbnx.config import settings
from isbnx.models import OCRResult


class ISBNXRapidOCR:
    """RapidOCR (onnxruntime) OCR 引擎。

    根据 ``settings.ocr.ocr_model`` 切换精度模式。

    - ``"small"``: 快速启动，最小参数（默认）

    - ``"medium"``: 高精度 Det/Rec 模型


    """

    def __init__(self) -> None:
        try:
            from rapidocr import (  # noqa: PLC0415
                EngineType,
                LangDet,
                LangRec,
                ModelType,
                OCRVersion,
                RapidOCR,
            )
        except ImportError:
            raise ImportError("rapidocr 未安装，请执行 `uv add rapidocr`") from None

        if settings.ocr.ocr_model == "medium":
            params = {
                "Det.engine_type": EngineType.ONNXRUNTIME,
                "Det.lang_type": LangDet.CH,
                "Det.model_type": ModelType.MEDIUM,
                "Det.ocr_version": OCRVersion.PPOCRV6,
                "Rec.engine_type": EngineType.ONNXRUNTIME,
                "Rec.lang_type": LangRec.CH,
                "Rec.model_type": ModelType.MEDIUM,
                "Rec.ocr_version": OCRVersion.PPOCRV6,
                "Det.limit_side_len": settings.ocr.det_limit_side_len,
                "Cls.engine_type": EngineType.ONNXRUNTIME,
                "Cls.lang_type": LangDet.CH,
                "Cls.model_type": ModelType.MOBILE,
                "Cls.ocr_version": OCRVersion.PPOCRV5,
            }
        else:
            params = {
                "Det.engine_type": EngineType.ONNXRUNTIME,
                "Det.lang_type": LangDet.CH,
                "Det.model_type": ModelType.SMALL,
                "Det.ocr_version": OCRVersion.PPOCRV6,
                "Rec.engine_type": EngineType.ONNXRUNTIME,
                "Rec.lang_type": LangRec.CH,
                "Rec.model_type": ModelType.SMALL,
                "Rec.ocr_version": OCRVersion.PPOCRV6,
                "Det.limit_side_len": settings.ocr.det_limit_side_len,
                "Cls.engine_type": EngineType.ONNXRUNTIME,
                "Cls.lang_type": LangDet.CH,
                "Cls.model_type": ModelType.MOBILE,
                "Cls.ocr_version": OCRVersion.PPOCRV4,
            }

        # 可选：禁用方向分类器（ISBN 文字始终水平，无需分类）
        if not settings.ocr.use_cls:
            params["Global.use_cls"] = False

        # 可选：跳过文本检测（YOLO 已定位到 ISBN 区域，只需识别）
        if not settings.ocr.use_det:
            params["Global.use_det"] = False

        params["Global.log_level"] = "warning"
        self._engine = RapidOCR(params=params)

    def recognize(self, image: Image.Image) -> OCRResult:
        """对图片进行 OCR 识别，返回标准化结果。"""
        # 转为灰度图再复制到 3 通道，减少颜色冗余，OCR 文本识别只需亮度信息
        gray = np.asarray(image.convert("L"))  # (H, W)
        img = np.stack([gray] * 3, axis=-1)  # (H, W, 3)
        raw = self._engine(img)

        lines: list[str] = []
        if raw is not None:
            # rapidocr 3.x 返回 RapidOCROutput 对象（有 .txts 属性）
            # 同时也兼容旧版返回格式
            if hasattr(raw, "txts") and raw.txts:  # type: ignore[union-attr]
                for t in raw.txts:  # type: ignore[union-attr]
                    t = str(t).strip()
                    if t:
                        lines.append(t)
            elif isinstance(raw, tuple) and len(raw) >= 1 and raw[0]:
                for item in raw[0]:
                    if isinstance(item, (list, tuple)) and len(item) >= 2:
                        text = str(item[1]).strip()
                        if text:
                            lines.append(text)

        return OCRResult(lines=lines, rawocr=raw)
