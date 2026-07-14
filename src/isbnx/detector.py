"""ONNX 检测器：定位并裁剪 ISBN 区域。"""

from __future__ import annotations

import threading
import time
from functools import cached_property
from importlib.resources import files
from pathlib import Path
from typing import TYPE_CHECKING

from PIL import Image

from isbnx.config import SourceType, settings
from isbnx.models import BookInfo, Detect, ExtractResult, Locate, Meta, OCRResult
from isbnx.ocr.isbnx_pyzbar import ISBNXZbar
from isbnx.ocr.isbnx_rapiocr import ISBNXRapidOCR
from isbnx.utils.cip_rules import extract_cip_fields
from isbnx.utils.isbn_utils import ISBN_KEYWORD as _ISBN_KEYWORD
from isbnx.utils.isbn_utils import extract_isbn_from_lines, is_valid_isbn

if TYPE_CHECKING:
    import numpy as np
    import onnxruntime as ort

    from isbnx.config import Settings

# ── 常量 ──


def _nms_numpy(boxes: np.ndarray, scores: np.ndarray, iou_thres: float = 0.45) -> np.ndarray:
    """纯 numpy NMS，返回保留框的索引。"""
    import numpy as np

    if boxes.size == 0:
        return np.empty(0, dtype=np.int64)

    x1 = boxes[:, 0]
    y1 = boxes[:, 1]
    x2 = boxes[:, 2]
    y2 = boxes[:, 3]
    areas = np.clip(x2 - x1, 0.0, None) * np.clip(y2 - y1, 0.0, None)
    order = scores.argsort()[::-1]
    keep: list[int] = []

    while order.size > 0:
        i = int(order[0])
        keep.append(i)
        if order.size == 1:
            break

        rest = order[1:]
        xx1 = np.maximum(x1[i], x1[rest])
        yy1 = np.maximum(y1[i], y1[rest])
        xx2 = np.minimum(x2[i], x2[rest])
        yy2 = np.minimum(y2[i], y2[rest])
        w = np.clip(xx2 - xx1, 0.0, None)
        h = np.clip(yy2 - yy1, 0.0, None)
        inter = w * h
        union = areas[i] + areas[rest] - inter

        iou = np.zeros_like(inter, dtype=np.float32)
        np.divide(inter, union, out=iou, where=union > 0)
        order = rest[iou <= iou_thres]

    return np.asarray(keep, dtype=np.int64)


def _truncate_at_isbn_keyword(lines: list[str]) -> list[str]:
    """以 "ISBN" 关键词为中心截断 OCR 行，排除前面的无关数字干扰。

    找到 "ISBN" 关键词后，保留该关键词之前的所有行 + 该行从关键词起的内容
    + 之后的所有行（保持各行独立，不合并，避免数字跨行拼接引发假阳性）。
    如果没找到 "ISBN" 关键词，返回原始行。
    """
    found_idx: int | None = None
    for i, line in enumerate(lines):
        if _ISBN_KEYWORD.search(line):
            found_idx = i
            break

    if found_idx is None:
        return lines

    m = _ISBN_KEYWORD.search(lines[found_idx])
    truncated = lines[found_idx][m.start() :] if m else lines[found_idx]
    return lines[:found_idx] + [truncated] + lines[found_idx + 1 :]


# ── 全局共享检测器 ──

_global_detector: Detector | None = None
_global_detector_lock = threading.Lock()


def get_detector() -> Detector:
    """获取全局共享的 Detector 单例（线程安全，惰性创建）。"""
    global _global_detector
    if _global_detector is not None:
        return _global_detector
    with _global_detector_lock:
        if _global_detector is None:
            _global_detector = Detector()
    return _global_detector


def reset_detector() -> None:
    """清除全局 Detector 单例，下次调用 ``get_detector()`` 时将重新创建。"""
    global _global_detector
    with _global_detector_lock:
        _global_detector = None


class Detector:
    """ONNX 检测器，定位并裁剪 ISBN 区域。

    Args:
        config: 可选的配置对象。为 ``None`` 时回退到全局 ``settings``。
            传入独立配置可避免多线程共享全局 ``settings`` 的竞态问题。
    """

    def __init__(self, config: Settings | None = None) -> None:
        # 深拷贝配置快照，避免全局 settings 被 configure() 修改后影响已创建的 Detector
        src = config or settings
        self._config = src.model_copy(deep=True)
        # 预热：主动触发惰性加载，避免首次推理时等待
        _ = self._session  # 触发 @cached_property，加载 ONNX 模型
        _ = self._ocr  # 触发 @cached_property，加载 OCR 引擎

    @staticmethod
    def _whiten_background(image: Image.Image) -> Image.Image:
        """将图片灰度化，消除偏色对 ONNX 检测的影响。

        对任意底色（泛黄、泛灰等）的页面，先转为灰度消除色彩偏差，
        再转回 3 通道以满足 ONNX 模型输入要求。
        """
        gray = image.convert("L")
        return gray.convert("RGB")

    def detect(self, image: Image.Image) -> list[Detect]:
        """检测图片中的 ISBN 目标区域。

        对输入图片进行 ONNX 推理，返回所有满足置信度阈值的检测框。

        Args:
            image: 输入 PIL Image（RGB）。

        Returns:
            检测结果列表，按置信度降序排列。每个元素包含裁剪后的图片和元信息。
            未检测到目标时返回空列表。
        """
        image = self._whiten_background(image)
        tensor, scale, pad_x, pad_y = self._preprocess(image)
        output = self._run(tensor)
        boxes = self._pick_boxes(output)
        detects: list[Detect] = []
        for box, score, class_id in boxes:
            crop_box = self._scale_box(box, image_size=image.size, scale=scale, pad_x=pad_x, pad_y=pad_y)
            if crop_box is not None:
                detects.append(Detect(box=crop_box, image=image.crop(crop_box), score=score, class_id=class_id))
        return detects

    def process(
        self,
        image: Image.Image,
        source: str = "",
        source_type: SourceType = "image",
    ) -> ExtractResult:
        """一步完成 ONNX 检测 + ISBN 提取。

        流程：

        1. ONNX 模型检测所有 ISBN 区域
        2. 按置信度降序遍历各候选框，对每个候选：

           - **条形码** (class_id=2): 优先 pyzbar 解码，失败后 OCR 2x 放大识别
           - **文字类** (class_id=0/1): OCR 识别 → ISBN 关键词截断 → 提取

        3. 一旦提取到合法 ISBN 立即返回
        4. 所有候选均失败则返回最后一个候选的失败结果

        Args:
            image: 输入图片。
            source: 源文件路径（可选）。
            source_type: 源文件类型，``"pdf"`` / ``"image"`` / ``"archive"`` / ``"epub"`` / ``"mobi"``。

        Returns:
            包含 ISBN、定位信息、OCR 结果的 ``ExtractResult``。

        Note:
            ``success=True`` 仅当提取到 **合法** ISBN 时成立。
            即使 ONNX 检出了区域，OCR 失败时 ``success`` 仍为 ``False``。
        """

        t0 = time.perf_counter()

        detects = self.detect(image)

        if not detects:
            return ExtractResult(
                bookinfo=BookInfo(),
                meta=Meta(source=source, source_type=source_type),
                error="未检测到 ISBN 区域",
                elapsed=time.perf_counter() - t0,
                strict=self._config.strict,
            )

        # ── 循环各候选框 OCR，首次识别到 ISBN 即返回 ──
        last_detect = detects[-1]
        last_ocr_result: OCRResult | None = None

        for detect in detects:
            isbn_str: str | None = None
            ocr_result: OCRResult | None = None

            if detect.class_id == 2:
                # ── 条形码（bar）：优先条码解码 → OCR 2x 放大 ──
                barcode = self._barcode.decode(detect.image)
                if barcode and is_valid_isbn(barcode):
                    isbn_str = barcode
                else:
                    w, h = detect.image.size
                    ocr_img = (
                        detect.image.resize((w * 2, h * 2), Image.Resampling.LANCZOS)
                        if min(w, h) > 100
                        else detect.image
                    )
                    ocr_result = self._ocr.recognize(ocr_img)
                    if ocr_result is not None:
                        isbn_str = extract_isbn_from_lines(ocr_result.lines)
            else:
                # ── 文字类（cip / alone）：OCR → 字段提取 ──
                w, h = detect.image.size
                ocr_img = detect.image
                min_dim = self._config.ocr.min_input_dim
                min_side = min(w, h)
                if min_side > 0 and min_side < min_dim:
                    scale = (min_dim + min_side - 1) // min_side
                    ocr_img = ocr_img.resize((w * scale, h * scale), Image.Resampling.LANCZOS)
                max_dim = self._config.ocr.max_input_dim
                if max_dim > 0 and (w > max_dim or h > max_dim):
                    scale = min(max_dim / w, max_dim / h)
                    ocr_img = ocr_img.resize((round(w * scale), round(h * scale)), Image.Resampling.LANCZOS)

                ocr_result = self._ocr.recognize(ocr_img)
                if ocr_result is not None and ocr_result.lines:
                    if detect.class_id == 1:
                        # ── cip 类：走完整 CIP 版权页解析管线（书名/作者/出版社/日期/ISBN）──
                        bookinfo = extract_cip_fields(ocr_result.lines)
                        if bookinfo.isbn:
                            isbn_str = bookinfo.isbn
                    else:
                        # ── alone 类：ISBN 关键词截断 → 正则提取 ──
                        lines = _truncate_at_isbn_keyword(ocr_result.lines)
                        isbn_str = extract_isbn_from_lines(lines)

            # ── 校验 ISBN（BookInfo 内部通过 _isbn 缓存做验证）──
            candidate = BookInfo(isbn=isbn_str) if isbn_str else None
            if candidate and candidate.is_valid(strict=2):
                # 成功！立即返回，附带所有候选框
                locate = Locate(page=1, method="onnx", detect=detect, candidates=detects)
                return ExtractResult(
                    bookinfo=candidate,
                    meta=Meta(source=source, source_type=source_type),
                    locate=locate,
                    ocr=ocr_result,
                    elapsed=time.perf_counter() - t0,
                    strict=self._config.strict,
                )

            # 记录最后一个候选的信息，以便全部失败时返回
            last_detect = detect
            last_ocr_result = ocr_result

        # ── 所有候选框均失败 ──
        locate = Locate(page=1, method="onnx", detect=last_detect, candidates=detects)
        return ExtractResult(
            bookinfo=BookInfo(),
            meta=Meta(source=source, source_type=source_type),
            locate=locate,
            ocr=last_ocr_result,
            error="未能从裁剪区域提取到 ISBN",
            elapsed=time.perf_counter() - t0,
            strict=self._config.strict,
        )

    @cached_property
    def _barcode(self) -> ISBNXZbar:
        """条形码解码器实例（惰性初始化）。"""
        return ISBNXZbar()

    @cached_property
    def _ocr(self) -> ISBNXRapidOCR:
        """RapidOCR 引擎实例（惰性初始化）。"""
        return ISBNXRapidOCR(self._config)

    @cached_property
    def _session(self) -> ort.InferenceSession:
        import onnxruntime as ort

        sess_options = ort.SessionOptions()
        sess_options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        sess_options.intra_op_num_threads = self._config.detector.num_threads
        return ort.InferenceSession(
            str(self._model_path),
            sess_options,
            providers=["CPUExecutionProvider"],
        )

    @cached_property
    def _model_path(self) -> Path:
        # 包内路径（优先）
        p = files(__package__) / self._config.detector.model_path
        if p.is_file():
            return Path(str(p))
        # 回退：直接作为文件系统路径
        p = Path(self._config.detector.model_path)
        if p.is_file():
            return p.resolve()
        return p

    def _preprocess(self, image: Image.Image) -> tuple[np.ndarray, float, int, int]:
        import numpy as np

        target_w = self._config.detector.input_width
        target_h = self._config.detector.input_height
        width, height = image.size
        scale = min(target_w / width, target_h / height)
        resized_w = round(width * scale)
        resized_h = round(height * scale)
        resized = image.resize((resized_w, resized_h), Image.Resampling.BILINEAR)

        color = tuple(int(x) for x in self._config.detector.letterbox_color.split(","))
        canvas = Image.new("RGB", (target_w, target_h), color)
        pad_x = (target_w - resized_w) // 2
        pad_y = (target_h - resized_h) // 2
        canvas.paste(resized, (pad_x, pad_y))

        array = np.asarray(canvas, dtype=np.float32) / 255.0
        tensor = array.transpose(2, 0, 1)[None]
        return np.ascontiguousarray(tensor), scale, pad_x, pad_y

    def _run(self, tensor: np.ndarray) -> np.ndarray:
        import numpy as np

        input_name = self._session.get_inputs()[0].name
        outputs = self._session.run(None, {input_name: tensor})
        if not outputs:
            raise RuntimeError("ONNX 模型未返回任何输出")
        return np.asarray(outputs[0], dtype=np.float32)

    def _pick_boxes(self, output: np.ndarray) -> list[tuple[np.ndarray, float, int]]:
        """从 ONNX 输出中取所有满足置信度阈值的检测框。

        Returns:
            [(box, score, class_id), ...] 按置信度降序排列，可能为空。
            box: [x1, y1, x2, y2] **在 640x640 画布上的像素坐标**（已统一）。

        当前模型导出格式是 ``(1, 300, 6)``，每行依次为：
        ``[x1, y1, x2, y2, score, class_id]``。

        这是 end2end 模型的解码后输出，但导出时 ``nms=True`` 不可用，
        因此这里还需要对候选框手动做一次 NMS。
        """
        import numpy as np

        detections = np.asarray(output, dtype=np.float32)
        if detections.ndim == 3:
            detections = detections[0]
        if detections.ndim != 2:
            raise ValueError(f"不支持的 ONNX 输出形状: {output.shape}")

        if detections.shape[1] != 6:
            raise ValueError(f"不支持的 ONNX 输出形状: {output.shape}")

        scores = detections[:, 4]
        class_ids = detections[:, 5].astype(np.int64)
        mask = scores >= self._config.detector.conf_threshold
        if not mask.any():
            return []

        boxes = detections[mask, :4].astype(np.float32)
        scores = scores[mask]
        class_ids = class_ids[mask]

        results: list[tuple[np.ndarray, float, int]] = []
        # 按 class_id 分组做 NMS，避免不同类别互相抑制。
        for class_id in np.unique(class_ids):
            class_mask = class_ids == class_id
            class_boxes = boxes[class_mask]
            class_scores = scores[class_mask]
            keep = _nms_numpy(class_boxes, class_scores, iou_thres=0.45)
            for i in keep:
                results.append((class_boxes[i], float(class_scores[i]), int(class_id)))

        results.sort(key=lambda x: x[1], reverse=True)

        return results

    def _scale_box(
        self,
        box: np.ndarray,
        *,
        image_size: tuple[int, int],
        scale: float,
        pad_x: int,
        pad_y: int,
    ) -> tuple[int, int, int, int] | None:
        import numpy as np

        width, height = image_size
        x1, y1, x2, y2 = box.astype(float)
        pad = self._config.detector.padding
        x1 = (x1 - pad_x) / scale - pad
        y1 = (y1 - pad_y) / scale - pad
        x2 = (x2 - pad_x) / scale + pad
        y2 = (y2 - pad_y) / scale + pad

        left = max(0, min(width, int(np.floor(x1))))
        right = max(0, min(width, int(np.ceil(x2))))
        top = max(0, min(height, int(np.floor(y1))))
        bottom = max(0, min(height, int(np.ceil(y2))))

        if right - left < 1 or bottom - top < 1:
            return None
        return left, top, right, bottom
