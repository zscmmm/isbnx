"""全局配置模块"""

from typing import Any, Literal

from loguru import logger
from pydantic import BaseModel, field_validator
from pydantic_settings import BaseSettings

# ── 类型别名 ──
OCREngine = Literal["rapidocr"]
SourceType = Literal["pdf", "image", "archive", "epub"]

# ── 全局常量 ──
CORE_FIELDS = ("isbn",)


class OCRConfig(BaseModel):
    """OCR 引擎配置。

    Attributes:
        ocr_model: OCR 模型精度，``"small"``（快速，默认）或 ``"medium"``（高精度）。
        use_cls: 是否启用方向分类器。ISBN 文字始终水平，无需分类，关闭可省 ~100-300ms。
        use_det: 是否启用文本检测（Det）阶段。YOLO 已定位到 ISBN 区域，可跳过检测。
        det_limit_side_len: 检测模型输入图像短边缩放长度。
        max_input_dim: OCR 输入图片的最大边长（像素）。超过此值会等比例缩小。
        min_input_dim: OCR 输入图片的最小边长（像素）。低于此值会等比例放大。
    """

    ocr_model: Literal["small", "medium"] = "small"  # OCR 模型精度，small/medium
    use_cls: bool = False
    use_det: bool = True  # 是否启用 RapidOCR 文本检测（Det）。建议开启, 除非class_id = 0
    det_limit_side_len: int = 320
    max_input_dim: int = 960  # OCR 输入图片的最大边长（像素）。超过此值会等比例缩小。
    min_input_dim: int = 300  # OCR 输入图片的最小边长（像素）。低于此值会等比例放大。


class DetectorConfig(BaseModel):
    """检测器（ONNX YOLO）配置。

    Attributes:
        model_path: ONNX 模型路径。可以是相对路径、绝对路径或包内路径。
        conf_threshold: 检测置信度阈值 (0~1)。
        fallback_ratio: 文本回退时 ONNX 阈值乘数。
        padding: 检测框填充像素数。
        input_width: 模型输入宽度。
        input_height: 模型输入高度。
        letterbox_color: Letterbox 填充颜色 (R,G,B)。
        num_threads: ONNX 推理线程数。
    """

    model_path: str = "model/isbndetect_yolo.onnx"
    conf_threshold: float = 0.3
    fallback_ratio: float = 0.6
    padding: int = 20
    input_width: int = 640
    input_height: int = 640
    letterbox_color: str = "114,114,114"
    num_threads: int = 4

    @field_validator("conf_threshold", "fallback_ratio")
    @classmethod
    def _validate_threshold(cls, value: float) -> float:
        if not (0 <= value <= 1):
            raise ValueError("阈值必须在 0~1 之间")
        return value

    @field_validator("model_path")
    @classmethod
    def _validate_model_path(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("model_path 不能为空")
        return value


class PDFConfig(BaseModel):
    """PDF 页码定位配置。

    控制 ISBN 检测在 PDF 前页／后页的搜索范围（偏移量，1-indexed）。
    """

    front_start: int = 2
    front_end: int = 10
    back_start: int = 5
    back_end: int = 1


class ArchiveConfig(BaseModel):
    """压缩包（PDG）提取配置。"""

    pdg_min_count: int = 30
    """PDG 数量阈值：超过此值才触发 ISBN 提取。"""

    pdg_fallback_count: int = 5
    """兜底：无 bookinfo.dat/leg001.pdg 时，尝试前 N 个 PDG 作为图片识别。"""

    pdgview_path: str = "pdgview/PdgView.dll"  # 包内相对路径，用于解码pdg


class Settings(BaseSettings):
    """全局配置"""

    # ── 子配置 ──
    ocr: OCRConfig = OCRConfig()
    detector: DetectorConfig = DetectorConfig()
    pdf: PDFConfig = PDFConfig()
    archive: ArchiveConfig = ArchiveConfig()

    # ── 校验等级 ──
    strict: int = 3
    """提取结果校验严格等级（值越小越严格）。

    - ``1``: ISBN 和 SSID 都必须存在，且 ISBN 校验通过。
    - ``2``: ISBN 必须存在且校验通过。
    - ``3``: ISBN 校验通过 或 SSID 存在（默认）。
    """

    # ── 日志 ──
    log_level: str = "INFO"


settings = Settings()


def configure(**kwargs: Any) -> None:
    """全局运行时配置，覆盖已有设置。

    只修改内存中的 settings 对象，不会写入文件。
    支持嵌套配置（自动识别 BaseModel 子字段并逐项更新）。
    """
    for key, value in kwargs.items():
        if not hasattr(settings, key):
            logger.warning(f"未知配置项: {key}")
            continue

        target = getattr(settings, key)
        if isinstance(target, BaseModel) and isinstance(value, dict):
            # 先构建一份完整配置进行校验，校验通过才实际写入
            merged = target.model_dump()
            merged.update(value)
            validated = target.__class__.model_validate(merged)
            for nk, nv in validated.model_dump().items():
                setattr(target, nk, nv)
        elif isinstance(target, BaseModel) and isinstance(value, BaseModel):
            # 直接传入同类型对象
            try:
                setattr(settings, key, value)
            except (TypeError, ValueError) as e:
                logger.warning(f"配置项 {key} 赋值失败: {e}")
        else:
            # 普通字段
            expected_type = type(target)
            if not isinstance(value, expected_type):
                logger.warning(f"配置项 {key} 类型不匹配: 期望 {expected_type.__name__}, 实际 {type(value).__name__}")
                continue
            try:
                setattr(settings, key, value)
            except (TypeError, ValueError) as e:
                logger.warning(f"配置项 {key} 赋值失败: {e}")
