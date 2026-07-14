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

# 优先从文件名提取（跳过内容扫描，更快）
result = extract("9787123456789_三体.epub", filename=True)
```

### CLI

```bash
isbnx cover.png          # 提取并打印结果
isbnx --json book.pdf    # JSON 格式输出
isbnx --strict 2 book.zip
isbnx --filename 9787123456789.epub  # 从文件名提取
```

---

## 支持格式

| 类型 | 格式 | 提取策略 |
|------|------|----------|
| 图片 | PNG / JPG / WebP / BMP / PDG | ONNX 检测 → OCR / 条码 |
| PDF | PDF | 文本搜索 / 渲染检测 |
| EPUB | EPUB | OPF 元数据 → XHTML 扫描 |
| MOBI | MOBI | EXTH 元数据 → 文本扫描 |
| 压缩包 | ZIP / RAR / 7Z / UVZ | meta.xml → bookinfo.dat → PDG 图片 |

---

## 批量处理

对整个目录进行批量 ISBN 提取与文件整理：

```python
from isbnx.batch import Batch

result = Batch(
    source_dir="D:/books",
    success_dir="D:/books/done",
    failed_dir="D:/books/unrecognized",
).run()
print(result)
```

也可配合 `BatchConfig` 自定义行为：

```python
from isbnx.batch import Batch, BatchConfig

config = BatchConfig(
    rename_mode=3,         # 替换旧标识再追加（默认）
    extensions={".epub", ".pdf"},
    max_workers=4,
    keep_tree=True,        # 保留源目录结构
)
result = Batch(
    source_dir="D:/books",
    success_dir="D:/done",
    failed_dir="D:/fail",
    config=config,
    try_run=True,          # 先预览不实际移动
).run()
```

支持 4 种重命名模式、多线程并行、文件去重、干运行预览、CSV 报告导出、进度回调、优雅终止和源目录结构保留。

---

## 详细文档

完整文档、API 参考、提取流程请访问：

👉 **[isbnx.readthedocs.org](https://isbnx.readthedocs.org)**
