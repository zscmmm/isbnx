# EPUB 提取流程

从 EPUB 文件中提取 ISBN，纯文本扫描，无需 OCR。

## 流程概述

```mermaid
graph TD
    A[EPUB 文件] --> B[打开 ZIP]
    B --> C[定位 OPF 文件]
    C --> D{找到 OPF?}
    D -->|是| E[扫描 OPF]
    D -->|否| F
    E --> G{找到 ISBN?}
    G -->|是| H[返回成功]
    G -->|否| F[列出文本文件]
    F --> I[版权页优先]
    I --> J[逐文件扫描]
    J --> K{字节预过滤}
    K -->|无 ISBN 关键字| J
    K -->|有 ISBN 关键字| L[解码+正则]
    L --> M{找到 ISBN?}
    M -->|是| H
    M -->|否| J
    J -->|全部扫描完| N[返回失败]
```

## 特点

- **纯文本扫描** — 无需 ONNX 检测和 OCR，速度极快（通常 1-10ms）
- **字节级预过滤** — 先检查文件是否含 `b"ISBN"` 或 `b"978"`，避免无效解码
- **版权页优先** — 优先扫描 copyright/titlepage/colophon 等文件
- **多编码支持** — 自动检测 utf-8 / utf-16-le / gb18030 / big5
