# 检测器

ONNX YOLO 检测器，定位并裁剪 ISBN 区域，支持条形码和文字类识别。

当前仓库使用的 ONNX 模型输出为 ``(1, 300, 6)``，每行格式固定为：
``[x1, y1, x2, y2, score, class_id]``。
这是 end2end 解码后的候选框输出，但导出时 ``nms=True`` 不可用，
因此检测器会对这些候选框再执行一次按类 NMS。

::: isbnx.detector.Detector
    options:
        members:
            - __init__
            - detect
            - process
            - crop_and_save

::: isbnx.detector.get_detector
