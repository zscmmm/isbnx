# 图片提取流程

从单张图片文件中提取 ISBN 的完整流程。

## 流程概述

```mermaid
graph TD
    A[输入图片] --> B[load_image]
    B --> C[ONNX YOLO 检测]
    C --> D{检测到区域?}
    D -->|否| E[返回失败]
    D -->|是| F[遍历候选框]
    F --> G{class_id}
    G -->|2 条形码 bar| H[pyzbar 解码]
    G -->|0 独立文字 alone / 1 CIP 页文字 cip| I[OCR 识别]
    H --> J{解码成功?}
    J -->|是| K[校验 ISBN]
    J -->|否| I
    I --> L[关键词截断]
    L --> M[extract_isbn]
    M --> K
    K --> N{ISBN 有效?}
    N -->|是| O[返回成功]
    N -->|否| F
    F -->|全部失败| P[返回失败]
```

## 关键步骤

1. **图片加载** — `load_image()` 统一处理路径/bytes/ndarray/PIL 输入，自动 EXIF 矫正
2. **ONNX 检测** — `Detector.detect()` 预处理 → 推理 → 坐标映射 → 裁剪候选区域
3. **检测类别** — class_id 映射：`0=alone`（独立 ISBN 文字）、`1=cip`（CIP 页 ISBN）、`2=bar`（条形码）。条形码优先 pyzbar 解码，失败再走 OCR；文字类直接 OCR
4. **OCR 识别** — RapidOCR 识别文字，ISBN 关键词截断，正则提取
5. **ISBN 校验** — `BookInfo.is_valid()` 按严格等级校验
