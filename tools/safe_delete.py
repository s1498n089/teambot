"""刪整段程式碼前的安全檢查 —— 先告訴你「這個區間裡有哪些東西會一起消失」。

## 為什麼有這支工具

2026-07-27 同一小時內犯了兩次一模一樣的錯:

    刪 a2a.py 的 AgentRegistry     → 連帶刪掉夾在中間的 now_iso  → 測試 NameError
    刪 test_api_behavior 的 TestRegister → 連帶刪掉 collect_sse_frames → 測試 NameError

兩次都是同一個手法出的事:**用「從這個 class 刪到下一個 class」的行號區間刪東西**。
那個區間看起來只裝著一樣東西,實際上還夾著別的定義。

第一次踩到之後我在聊天室寫了教訓、也講了防法(「刪之前先列出區間裡有哪些頂層定義」)——
然後二十分鐘後又犯了。**寫下教訓不等於改變行為**,所以這次把它變成一支會執行的工具:
記性靠不住,檢查步驟得自己站得住。

## 用法

    uv run tools/safe_delete.py <檔案> <起始行> <結束行>

它不刪任何東西,只列出那個區間裡的頂層定義(函式、類別、常數),讓你確認
「這些是不是我真的想刪的全部」。確認完再自己動手刪。
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path


def definitions_in_range(source: str, start_line: int, end_line: int) -> list[str]:
    """列出這個行號區間裡的頂層定義(1-indexed,含頭含尾)。"""
    tree = ast.parse(source)
    found = []
    for node in tree.body:
        line = getattr(node, "lineno", None)
        if line is None or not (start_line <= line <= end_line):
            continue
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            found.append(f"def {node.name}()  第 {line} 行")
        elif isinstance(node, ast.ClassDef):
            found.append(f"class {node.name}  第 {line} 行")
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name):
                    found.append(f"{target.id} = ...  第 {line} 行")
    return found


def main() -> int:
    if len(sys.argv) != 4:
        print(__doc__)
        return 1

    path = Path(sys.argv[1])
    start_line = int(sys.argv[2])
    end_line = int(sys.argv[3])
    source = path.read_text(encoding="utf-8")

    found = definitions_in_range(source, start_line, end_line)

    print(f"{path} 第 {start_line}~{end_line} 行裡有 {len(found)} 個頂層定義:")
    for item in found:
        print(f"  · {item}")

    if len(found) > 1:
        print()
        print("★ 超過一個。確認這些【全部】都是你要刪的,")
        print("  不然就是有東西夾在中間 —— 那正是踩過兩次的那個坑。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
