#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""批量回归测试：自动发现并运行 test_smoke_v*.py，汇总结果。

用法:
    python run_tests.py                跑到全部
    python run_tests.py v18 v19 v20    只跑指定版本（提速，改哪儿跑哪儿）
    python run_tests.py --list         仅列出可用测试
    python run_tests.py --fail-fast    遇错即停

退出码: 有任一失败 -> 1，否则 0。
"""

import os
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PY = (r"C:\Users\29227\.workbuddy\binaries\python\envs\screentrans\Scripts\python.exe")
if not Path(PY).exists():
    PY = sys.executable


def discover():
    """返回 [(版本号, 路径)]，按版本号升序。"""
    items = []
    for p in ROOT.glob("test_smoke_v*.py"):
        m = re.search(r"test_smoke_v(\d+)\.py$", p.name)
        if m:
            items.append((int(m.group(1)), p))
    return sorted(items)


def summarize(out):
    """从输出里提取一句话摘要。"""
    m = re.search(r"通过\s+(\d+)/(\d+)", out)
    if m:
        return f"{m.group(1)}/{m.group(2)}"
    if "ALL SMOKE TESTS PASSED" in out:
        return "全部通过"
    m = re.search(r"FAILED:\s*(.+)", out)
    if m:
        return "FAILED " + m.group(1)[:60]
    return "?"


def main():
    argv = sys.argv[1:]
    if "--list" in argv:
        for v, p in discover():
            print(f"  v{v:<3} {p.name}")
        return

    fail_fast = "--fail-fast" in argv
    wanted = {a.lstrip("v") for a in argv if not a.startswith("--")}

    tests = discover()
    if wanted:
        tests = [(v, p) for v, p in tests if str(v) in wanted]
        if not tests:
            print("没有匹配的测试（用 --list 查看可用项）")
            sys.exit(1)

    env = dict(os.environ)
    env.setdefault("QT_QPA_PLATFORM", "offscreen")

    print(f"运行 {len(tests)} 个测试（Python: {PY}）\n" + "-" * 56)
    fails = []
    t_all = time.time()
    for v, path in tests:
        t0 = time.time()
        r = subprocess.run([PY, str(path)], cwd=str(ROOT), env=env,
                           capture_output=True, text=True, errors="replace")
        out = (r.stdout or "") + (r.stderr or "")
        dt = time.time() - t0
        ok = r.returncode == 0
        mark = "OK  " if ok else "FAIL"
        print(f"[{mark}] v{v:<3} {summarize(out):<12} {dt:5.1f}s")
        if not ok:
            fails.append((v, out))
            # 打印失败详情，方便定位
            for line in out.splitlines():
                if "FAIL" in line or "Traceback" in line or "Error" in line:
                    print("       " + line.strip()[:110])
            if fail_fast:
                break
    total = time.time() - t_all
    print("-" * 56)
    if fails:
        print(f"失败 {len(fails)} 个: " + ", ".join(f"v{v}" for v, _ in fails))
        print(f"总耗时 {total:.1f}s")
        sys.exit(1)
    print(f"全部通过（{len(tests)} 个，总耗时 {total:.1f}s）")


if __name__ == "__main__":
    main()
