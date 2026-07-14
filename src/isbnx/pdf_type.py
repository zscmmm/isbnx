"""PDF 类型检测模块。

独立于 pdf_inspector，使用 PyMuPDF 直接采样页面文本，判断 PDF 类型：

- ``text_based``: 大部分页面含文本
- ``scanned``: 大部分页面无文本（扫描件）
- ``mixed``: 部分页面含文本
"""

from __future__ import annotations

import fitz  # PyMuPDF


def detect_pdf_type(doc: fitz.Document) -> str:
    """检测 PDF 类型：text_based / scanned / mixed。

    采样前中后页，根据有文本页面的占比判断。

    Args:
        doc: 已打开的 PyMuPDF Document 对象。

    Returns:
        ``"text_based"`` / ``"scanned"`` / ``"mixed"``。
    """
    try:
        total = doc.page_count
        if total == 0:
            return "scanned"

        # 采样：跳过封面（首页），从第 2 页到末页均匀取最多 5 页
        sample: set[int] = set()
        if total > 1:
            n_samples = min(5, total - 1)
            for k in range(n_samples):
                idx = 1 + k * (total - 2) // max(1, n_samples - 1)
                sample.add(idx)
        else:
            sample.add(0)

        text_pages = 0
        for i in sample:
            text = str(doc[i].get_text("text")).strip()
            if text:
                text_pages += 1

        ratio = text_pages / len(sample)
        if ratio >= 0.5:
            return "text_based"
        if ratio > 0:
            return "mixed"
    except (RuntimeError, ValueError):
        # 第三方库 (fitz) 页访问无 LBYL 替代，异常处理合理
        pass

    return "scanned"
