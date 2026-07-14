# 入口 ISBNX

`isbnx.isbnx.ISBNX` 是统一的 ISBN 提取入口，提供按后缀自动分发的 `extract()`，以及用于精细控制的 `from_image()` / `from_pdf()` / `from_epub()` / `from_mobi()` / `from_archive()`。

`extract()` 只做轻量后缀判断，不做内容嗅探；如果你已经知道文件类型，也可以直接调用对应方法。

批量处理请使用 [`Batch`](batch.md) 类。

::: isbnx.isbnx.ISBNX
