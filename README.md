# isbnx

> 从 PDF、图片、EPUB、MOBI 和压缩包中智能提取 ISBN 号

[![Python](https://img.shields.io/badge/python-≥3.11-blue)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![Documentation](https://img.shields.io/badge/docs-mkdocs-blue)](https://isbnx.readthedocs.org)

---

## 安装

```bash
pip install isbnx
# 或
uv add isbnx
```

### 系统依赖

**pyzbar**（条形码解码）:

- **Windows**: 需要安装 Visual C++ 运行库
- **macOS**: 设置环境变量 `export DYLD_LIBRARY_PATH=/opt/homebrew/lib:$DYLD_LIBRARY_PATH`

**pdf-inspector**（可选，PDF 类型检测）: [参考安装](https://github.com/firecrawl/pdf-inspector)

---

## 快速开始

```python
from isbnx import extract

result = extract("cover.png")
if result.success:
    print(result.bookinfo.isbn13)  # 9787123456789
```

### CLI

```bash
isbnx cover.png          # 提取并打印结果
isbnx --json book.pdf    # JSON 格式输出
isbnx --strict 2 book.zip
```

---

## 支持格式

| 类型 | 格式 | 提取策略 |
|------|------|----------|
| 图片 | PNG / JPG / WebP / BMP / PDG | ONNX 检测 → OCR / 条码 |
| PDF | PDF | 文本搜索 / 渲染检测 |
| EPUB | EPUB | OPF 元数据 → XHTML 扫描 |
| MOBI | MOBI | EXTH 元数据 → 文本扫描 |
| 压缩包 | ZIP / RAR / UVZ | meta.xml → bookinfo.dat → PDG 图片 |

---

## 详细文档

完整文档、API 参考、提取流程请访问：

👉 **[isbnx.readthedocs.org](https://isbnx.readthedocs.org)**
