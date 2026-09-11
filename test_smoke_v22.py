"""v22 冒烟测试: 解释器自动探测（pylocator） + 模型自动下载（model_fetch）

背景:
  为了把仓库放到 GitHub 上当作品集，做了两件"可移植化"改造：
    1. 6 个脚本里写死的 `C:\\Users\\<某人>\\...python.exe` 全部改走
       pylocator 自动探测（SCREENTRANS_PY -> pyenv.json -> 常见共享环境/.venv -> PATH）
    2. 精简版发布包不带 842MB 模型，改为程序首次启动自动下载中英双向

  这两件事都有踩坑点，必须用测试钉住：
    - 探测结果必须"路径真实存在"，且本机仍要命中原来那个 venv（否则
      RapidOCR 会静默退回系统 OCR、性能表现对不上 README）
    - pyenv.json 里的失效路径、空字符串都要被跳过（install 脚本换机后常见）
    - **本机已有模型时绝不能触发下载**（否则一启动就偷偷下 165MB）
    - 信号必须真接上：pyqtSignal 漏 connect 是静默失效（项目踩过）

运行: venv python test_smoke_v22.py
"""
import json
import os
import sys
import tempfile
import zipfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent))

import model_fetch as mf
import pylocator

PASS = []


def check(name, cond, detail=""):
    PASS.append((name, bool(cond)))
    mark = "ok  " if cond else "FAIL"
    print(f"  [{mark}] {name}" + (f"  -> {detail}" if detail else ""))


# ============ A. 解释器探测 ============
print("\n== A. pylocator 解释器探测 ==")

cands = pylocator.candidate_pythons()
check("A1.候选非空", bool(cands), f"{len(cands)} 个")
check("A2.候选都真实存在", all(Path(p).exists() for p in cands),
      str([p for p in cands if not Path(p).exists()][:2]))
check("A3.候选已去重", len(cands) == len(set(cands)),
      f"{len(cands)} vs {len(set(cands))}")

trusted = pylocator.candidate_pythons(include_path=False)
check("A4.trusted 集是子集", set(trusted) <= set(cands),
      f"trusted={len(trusted)} all={len(cands)}")

# 本机仍要命中装了本项目依赖的那个解释器（回归护栏）
py_qt = pylocator.find_python("PyQt5")
check("A5.能命中装了 PyQt5 的解释器", bool(py_qt), str(py_qt))
check("A6.can_import 正面", pylocator.can_import(sys.executable, "sys"))
check("A7.can_import 反面",
      not pylocator.can_import(sys.executable, "definitely_not_a_module_xyz"))

# 环境变量覆盖：临时指向当前解释器，应排在候选首位
os.environ["SCREENTRANS_PY"] = sys.executable
try:
    c2 = pylocator.candidate_pythons()
    check("A8.SCREENTRANS_PY 生效", c2 and c2[0] == sys.executable,
          c2[0] if c2 else "(空)")
finally:
    os.environ.pop("SCREENTRANS_PY", None)

# pyenv.json: 空串跳过 / 失效路径跳过 / 相对路径解析
with tempfile.TemporaryDirectory() as td:
    tdp = Path(td)
    (tdp / "pyenv.json").write_text(json.dumps({"python": ""}), encoding="utf-8")
    check("A9.空 python 被跳过", pylocator._from_pyenv_json([tdp]) == [])

    (tdp / "pyenv.json").write_text(
        json.dumps({"python": "C:/definitely/not/here/python.exe"}),
        encoding="utf-8")
    check("A10.失效路径被跳过", pylocator._from_pyenv_json([tdp]) == [])

    real = Path(sys.executable)
    rel = os.path.relpath(real, tdp)
    (tdp / "pyenv.json").write_text(json.dumps({"python": rel}), encoding="utf-8")
    got = pylocator._from_pyenv_json([tdp])
    check("A11.相对路径可解析", len(got) == 1 and Path(got[0]).exists(),
          got[0] if got else "(空)")

    (tdp / "pyenv.json").write_text("{ 坏 JSON", encoding="utf-8")
    check("A12.坏 JSON 不抛异常", pylocator._from_pyenv_json([tdp]) == [])

# 仓库里的 pyenv.json 不该再写死个人路径
repo_pyenv = Path(__file__).parent / "pyenv.json"
raw = repo_pyenv.read_text(encoding="utf-8")
check("A13.pyenv.json 无写死个人路径",
      "Users\\\\" not in raw and "Users\\" not in raw.replace("\\\\", "\\")
      or "29227" not in raw,
      "已入库的 pyenv.json 不含本机用户名")

# 源码里不该再有写死的 C:\Users\<name>\... 解释器路径
# （排除测试脚本本身：它必须包含这个字面量才能做匹配）
NEEDLE = "C:" + "\\Users\\"
hard = []
for f in Path(__file__).parent.glob("*.py"):
    if f.name.startswith("test_smoke_"):
        continue
    try:
        for i, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            if NEEDLE in line or NEEDLE.replace("\\", "/") in line:
                hard.append(f"{f.name}:{i}")
    except Exception:
        pass
for f in Path(__file__).parent.glob("*.bat"):
    try:
        if NEEDLE in f.read_text(encoding="utf-8", errors="ignore"):
            hard.append(f.name)
    except Exception:
        pass
check("A14.源码内无写死 C:\\Users\\ 路径", not hard, str(hard[:3]))


# ============ B. 模型下载 ============
print("\n== B. model_fetch 模型获取 ==")

check("B1.human 单位换算",
      mf.human(512) == "512.0B" and mf.human(2048) == "2.0KB"
      and mf.human(5 * 1024 * 1024) == "5.0MB",
      f"{mf.human(512)} / {mf.human(2048)} / {mf.human(5*1024*1024)}")

idx = [
    {"code": "translate-zh_en", "from_code": "zh", "to_code": "en",
     "links": ["https://example.com/translate-zh_en-1_9.argosmodel"]},
    {"code": "translate-en_zh", "from_code": "en", "to_code": "zh",
     "links": ["https://example.com/translate-en_zh-1_9.argosmodel"]},
    {"code": "translate-ja_en", "from_code": "ja", "to_code": "en",
     "links": ["https://example.com/translate-ja_en-1_1.argosmodel"]},
]
check("B2.find_package", mf.find_package(idx, "translate-zh_en") is not None)
check("B3.find_package 未命中", mf.find_package(idx, "translate-xx_yy") is None)

found, missing = mf.resolve_packages(idx, ("zh", "en"))
codes = [p["code"] for p in found]
check("B4.CORE(zh,en) 展开为双向", codes == ["translate-zh_en", "translate-en_zh"],
      str(codes))
check("B5.en 不产生 en_en", "translate-en_en" not in codes)
check("B6.无多余缺失项", missing == [], str(missing))

found2, missing2 = mf.resolve_packages(idx, ("zh", "ko"))
check("B7.缺失项去重且如实上报", missing2 == ["translate-ko_en", "translate-en_ko"],
      str(missing2))

# install_package: 造一个"包内多套一层目录"的 zip，验证靠 metadata.json 定位
with tempfile.TemporaryDirectory() as td:
    tdp = Path(td)
    pkg = tdp / "translate-zh_en-1_9.argosmodel"
    inner = tdp / "build" / "nested" / "model"
    inner.mkdir(parents=True)
    (inner / "metadata.json").write_text('{"from_code":"zh"}', encoding="utf-8")
    (inner / "model.bin").write_bytes(b"\x00" * 32)
    with zipfile.ZipFile(pkg, "w") as z:
        z.write(inner / "metadata.json", "nested/model/metadata.json")
        z.write(inner / "model.bin", "nested/model/model.bin")

    dest = tdp / "models"
    ok1 = mf.install_package(pkg, dest)
    check("B8.install_package 成功", ok1)
    check("B9.目录名归一化",
          (dest / "translate-zh_en-1_9" / "metadata.json").exists(),
          str(sorted(p.name for p in dest.glob("translate-*"))))
    check("B10.无 _tmp 残留", not list(dest.glob("*_tmp")))

    ok2 = mf.install_package(pkg, dest)          # 幂等
    check("B11.重复安装幂等", ok2 and len(list(dest.glob("translate-*"))) == 1)

    check("B12.installed_codes", mf.installed_codes(dest) == ["translate-zh_en-1_9"])
    check("B13.models_missing 空目录", mf.models_missing(tdp / "nope"))
    check("B14.models_missing 有模型", not mf.models_missing(dest))

# 索引缓存可读（不联网）
local_idx = Path(__file__).parent / "argos_index.json"
cached = mf.load_index(local_idx)
check("B15.本地索引缓存可读", isinstance(cached, list) and len(cached) > 10,
      f"{len(cached)} 个包")
check("B16.索引镜像有多个（含国内代理）", len(mf.INDEX_URLS) >= 2,
      str(mf.INDEX_URLS[1][:38]))

# 真实场景：本机已有模型 -> 不该触发下载
from translator import runtime_model_dir
rt = runtime_model_dir()
n_local = len(mf.installed_codes(rt))
check("B17.本机模型已就绪", not mf.models_missing(rt), f"{n_local} 个模型 @ {rt}")


# ============ C. 接线（配置 + 信号） ============
print("\n== C. main / config 接线 ==")

from config import DEFAULT_CONFIG
check("C1.默认开启自动下载", DEFAULT_CONFIG.get("auto_fetch_models") is True)

import main as main_mod
check("C2.有进度信号", hasattr(main_mod.ScreenTranslatorApp, "sig_model_status"))
check("C3.有下载方法", hasattr(main_mod.ScreenTranslatorApp, "_auto_fetch_models"))
check("C4.有进度回调", hasattr(main_mod.ScreenTranslatorApp, "_on_model_status"))

# 环境变量闸门：离线 worker 子进程不应重复下载
import offline_translate as ot
os.environ["SCREEN_TRANSLATOR_NO_AUTO_FETCH"] = "1"
try:
    check("C5.子进程闸门生效",
          ot._auto_fetch_models(Path(tempfile.gettempdir()) / "nope") is False)
finally:
    os.environ.pop("SCREEN_TRANSLATOR_NO_AUTO_FETCH", None)


# ============ 汇总 ============
failed = [n for n, ok in PASS if not ok]
print(f"\n通过 {len(PASS) - len(failed)}/{len(PASS)}")
for n in failed:
    print("  [FAIL]", n)
raise SystemExit(1 if failed else 0)
