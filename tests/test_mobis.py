"""简单测试：遍历 tests/data/mobi/，跑 MOBI ISBN 提取，打印结果。"""

from pathlib import Path

from isbnx import ISBNX

MOBIS_DIR = Path(__file__).parent / "data" / "mobi"


def main() -> None:
    mobis = sorted(MOBIS_DIR.glob("*.mobi"))
    if not mobis:
        print(f"[ERR] 未找到测试 MOBI: {MOBIS_DIR}")
        return

    extractor = ISBNX()
    print(f"[INFO] 共 {len(mobis)} 个 MOBI\n")

    total_ok = 0
    for i, mobi_path in enumerate(mobis, 1):
        result = extractor.from_mobi(mobi_path)

        icon = "[OK]" if result.success else "[FAIL]"
        total_ok += 1 if result.success else 0

        print(f"{'=' * 60}")
        print(f"  {icon} [{i}/{len(mobis)}] {mobi_path.name}")
        print(f"{'=' * 60}")
        print(result)
        print()

    print(f"{'=' * 60}")
    print(f"  总计: {len(mobis)} 个, 成功: {total_ok}, 失败: {len(mobis) - total_ok}")


if __name__ == "__main__":
    import time

    t0 = time.perf_counter()
    main()
    print(f"\n[INFO] 耗时: {time.perf_counter() - t0:.2f} 秒")
