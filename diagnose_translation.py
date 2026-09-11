# -*- coding: utf-8 -*-
"""屏幕翻译 - 翻译链路诊断脚本

用法（两种任选）:
    双击 诊断翻译.bat
    或命令行: python diagnose_translation.py

依次检查: 配置 -> 模型 -> worker 启动 -> 翻译实测 -> 程序日志，
最后给出结论与建议。全程只读，不修改任何程序文件。
"""
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

OK = "[OK]"
BAD = "[X]"
WARN = "[!]"

BASE = Path(__file__).resolve().parent
PYENV = BASE / "pyenv.json"
SCRIPT = BASE / "offline_translate.py"

print("=" * 62)
print("屏幕翻译 - 翻译链路诊断")
print("=" * 62)


def section(name):
    print(f"\n---- {name} ----")


# ---------- 1. Python 解释器 ----------
section("1. Python 解释器")
py = None
if PYENV.exists():
    try:
        py = json.loads(PYENV.read_text(encoding="utf-8")).get("python")
        print(f"{OK} pyenv.json -> {py}")
        if not Path(py).exists():
            print(f"{BAD} 该解释器不存在! 程序将无法启动翻译 worker")
            py = None
    except Exception as e:
        print(f"{BAD} pyenv.json 解析失败: {e}")
else:
    print(f"{WARN} 找不到 pyenv.json，将尝试系统 python")
if py is None:
    py = sys.executable
    print(f"{WARN} 回退使用: {py}")

# ---------- 2. 离线模型 ----------
section("2. 离线翻译模型")
rt = Path(os.environ.get("LOCALAPPDATA", "")) / "ScreenTranslator" / "models"
if rt.exists():
    pkgs = sorted(p.name for p in rt.glob("translate-*"))
    print(f"{OK if pkgs else BAD} 运行模型目录: {rt}")
    print(f"    模型数量: {len(pkgs)}  ->  {pkgs}")
    if not pkgs:
        print(f"{BAD} 没有任何模型! 请重新运行 install_models.py 或重装程序")
else:
    print(f"{BAD} 模型目录不存在: {rt}")

# ---------- 3. worker 启动 + 翻译实测 ----------
section("3. worker 启动与翻译实测（约需 10~60 秒）")
if not SCRIPT.exists():
    print(f"{BAD} 找不到 {SCRIPT}，无法测试 worker")
else:
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    t0 = time.time()
    proc = subprocess.Popen(
        [py, str(SCRIPT), "--serve"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, encoding="utf-8",
        errors="replace", bufsize=1, env=env, cwd=str(BASE),
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    ready_line = None
    try:
        ready_line = proc.stdout.readline()
        ready_dt = time.time() - t0
        if '"ready"' in ready_line:
            print(f"{OK} worker 就绪，耗时 {ready_dt:.1f} 秒（含全部模型加载）")
        else:
            print(f"{BAD} worker 首行输出异常: {ready_line[:200]}")
    except Exception as e:
        print(f"{BAD} worker 启动失败: {e}")

    if ready_line and '"ready"' in ready_line:
        cases = [
            ("短句", "Hello world, this is a test.", "en", "zh"),
            ("你的游戏文本", "23.Cane's Favour: Complete Ale for Sale at least "
             "three times and see at least three private shows in total.",
             "en", "zh"),
            ("长文本", ("The quick brown fox jumps over the lazy dog. " * 10)
             + "Unlock the Backyard Barn through Ranch Patrol.", "en", "zh"),
        ]
        for name, text, src, dst in cases:
            req = {"cmd": "translate", "text": text,
                   "from": src, "to": dst, "id": 1}
            t0 = time.time()
            try:
                proc.stdin.write(json.dumps(req) + "\n")
                proc.stdin.flush()
                line = proc.stdout.readline()
                dt = time.time() - t0
                resp = json.loads(line) if line.strip() else {}
                if resp.get("ok"):
                    out = (resp.get("text") or "")[:60].replace("\n", " ")
                    print(f"{OK} {name}: {dt:.1f} 秒 -> {out}...")
                else:
                    print(f"{BAD} {name}: {dt:.1f} 秒 -> 失败: {str(resp)[:200]}")
            except Exception as e:
                print(f"{BAD} {name}: 请求失败: {e}")
                break
    try:
        proc.kill()
    except Exception:
        pass
    err = ""
    try:
        err = proc.stderr.read() or ""
    except Exception:
        pass
    if err.strip():
        print(f"{WARN} worker stderr（末尾 500 字）:")
        print("    " + err[-500:].replace("\n", "\n    "))

# ---------- 4. 程序运行日志 ----------
section("4. 程序日志分析")


def _find_log_file():
    """与 main._resolve_log_dir 相同的解析顺序。"""
    exe_dir = Path(sys.executable).parent
    cands = []
    proj = exe_dir.parent.parent
    if (proj / "README.md").exists():
        cands.append(proj / "logs" / "app.log")
    cands.append(exe_dir / "logs" / "app.log")
    cands.append((Path(os.environ.get("LOCALAPPDATA", "")) / "ScreenTranslator"
                  / "logs" / "app.log"))
    for p in cands:
        if p.exists():
            return p
    return cands[0]


log_file = _find_log_file()
if not log_file.exists():
    print(f"{WARN} 没有程序日志: {log_file}")
    print("    （程序还没运行过，或日志功能是旧版本）")
else:
    lines = log_file.read_text(encoding="utf-8", errors="replace").splitlines()
    # 找最后一次启动
    starts = [i for i, l in enumerate(lines) if "程序启动" in l]
    seg = lines[starts[-1]:] if starts else lines[-80:]
    print(f"{OK} 日志: {log_file}")
    print(f"    最近一次启动: {seg[0][:19] if seg else '?'}，本段共 {len(seg)} 行")

    done = [l for l in seg if "翻译完成" in l]
    print(f"    翻译完成次数: {len(done)}")
    if done:
        print(f"    最近 3 次:")
        for l in done[-3:]:
            print(f"      {l[:120]}")

    dropped = [l for l in seg if "丢弃过期" in l]
    if dropped:
        print(f"{WARN} 有 {len(dropped)} 条结果被当过期丢弃（旧版会有此问题，新版已修复）")

    timeout = [l for l in seg if "超时" in l or "Timeout" in l]
    if timeout:
        print(f"{BAD} 发现 {len(timeout)} 条超时记录（worker 卡死迹象）:")
        for l in timeout[-3:]:
            print(f"      {l[:120]}")

    errors = [l for l in seg if "[ERROR]" in l or "Traceback" in l or "异常" in l]
    if errors:
        print(f"{WARN} 发现 {len(errors)} 条错误日志（最近 3 条）:")
        for l in errors[-3:]:
            print(f"      {l[:140]}")

    if "Argos worker 就绪" in "\n".join(seg):
        i = next(j for j, l in enumerate(seg) if "Argos worker 就绪" in l)
        t_ready = seg[i][:19]
        print(f"{OK} worker 在 {t_ready} 就绪")

    if not done and len(seg) > 20:
        print(f"{BAD} 本段日志没有任何翻译完成记录 —— 如果当时你划了选区，")
        print("    说明翻译链路有断点，把本输出发给开发者。")

# ---------- 5. 结论 ----------
section("5. 结论与建议")
print("""
常见情况对照:
  A. 第 3 步翻译实测全部 OK，但程序界面长时间"翻译中" ->
     旧版 exe 的调度 bug（结果被当过期丢弃），用新版 exe 即可解决。
  B. 第 3 步 worker 就绪超过 60 秒或翻译超时 ->
     电脑硬盘/内存慢或被占用，关闭其他大程序后重试。
  C. 第 2 步模型缺失 -> 重新运行 install_models.py。
  D. 第 4 步日志显示"翻译完成"但界面没更新 -> 把日志末尾几行截图反馈。
""")
print("诊断结束。")
