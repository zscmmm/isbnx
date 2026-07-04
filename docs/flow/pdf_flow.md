# PDF 提取流程

从 PDF 文件中提取 ISBN，支持文本型 PDF 和扫描件。

## 流程概述

```mermaid
graph TD
    A[PDF 文件] --> B[pdf_inspector 分类]
    B --> C{PDF 类型}
    C -->|text_based| D[书签检测]
    C -->|scanned| D
    D --> E[生成候选页]
    E --> F[遍历候选页]
    F --> G[提取文本]
    G --> H{找到 ISBN?}
    H -->|是| I[返回成功]
    H -->|否| J[渲染页面为图片]
    J --> K[ONNX 检测]
    K --> L{找到 ISBN?}
    L -->|是| I
    L -->|否| F
    F -->|全部失败| M[返回失败]
```

## 关键步骤

1. **PDF 分类** — `pdf_inspector` 判断 PDF 为文本型或扫描型
2. **书签检测** — 按关键词"版权"/"封底"查找书签页，优先级最高
3. **候选页** — 前 2-10 页 + 后 5-1 页，书签页优先
4. **文本提取** — text_based 时 `page.get_text()` 后正则搜索 ISBN
5. **渲染+ONNX** — 扫描件/文本失败时渲染页面为图片，走 ONNX 检测
