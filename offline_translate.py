"""离线翻译子进程脚本（自包含，不依赖项目其他模块）

用法:
    python offline_translate.py "text" "from_code" "to_code"   # 翻译，输出译文
    python offline_translate.py --list                          # 输出已安装语言对

支持:
    - from_code=auto 时自动检测语言（中日韩/西里尔/泰文/阿拉伯/英文）
    - 多跳中转（pivot）：没有直达模型时经英语中转，如 ja->en->zh
    - 模型目录：优先用环境变量 ARGOS_PACKAGES_DIR，否则找 exe/脚本同级 assets/argos_models
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


def runtime_model_dir() -> Path:
    """模型实际加载目录（纯 ASCII，避免底层库打不开中文路径）。"""
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        return base / "ScreenTranslator" / "models"
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")
                ) / "argos-translate" / "packages"


def sync_bundled_models(dest: Path) -> bool:
    """把脚本/exe 同级 assets/argos_models 里的模型同步到运行目录。"""
    import json
    import shutil
    here = Path(__file__).parent
    cands = [here / "assets" / "argos_models",
             Path(sys.executable).parent / "assets" / "argos_models"]
    src = None
    for c in cands:
        try:
            if c.exists() and any(c.glob("translate-*")):
                src = c
                break
        except Exception:
            continue
    if not src:
        return False
    dest.mkdir(parents=True, exist_ok=True)
    state_file = dest / ".synced.json"
    synced = {}
    if state_file.exists():
        try:
            synced = json.loads(state_file.read_text(encoding="utf-8"))
        except Exception:
            synced = {}
    changed = False
    for pkg in sorted(src.glob("translate-*")):
        if pkg.name in synced:
            continue
        target = dest / pkg.name
        try:
            if target.exists():
                shutil.rmtree(target, ignore_errors=True)
            shutil.copytree(pkg, target)
            synced[pkg.name] = str(pkg)
            changed = True
        except Exception:
            pass
    if changed:
        try:
            state_file.write_text(json.dumps(synced, ensure_ascii=False, indent=1),
                                  encoding="utf-8")
        except Exception:
            pass
    return True


def setup_model_dir():
    """在 import argostranslate 之前完成的离线配置。"""
    # 禁止 stanza 联网下载分句模型（否则日/韩等模型首次翻译会卡在网络请求）
    os.environ.setdefault("ARGOS_STANZA_AVAILABLE", "0")
    os.environ.setdefault("STANZA_RESOURCES_DIR", "")

    rt = runtime_model_dir()
    try:
        if not (rt.exists() and any(rt.glob("translate-*"))):
            sync_bundled_models(rt)
        if rt.exists() and any(rt.glob("translate-*")):
            os.environ["ARGOS_PACKAGES_DIR"] = str(rt)
            return
    except Exception:
        pass
    # 退化：直接用同级 assets（含中文路径时可能失败，但至少给了机会）
    here = Path(__file__).parent / "assets" / "argos_models"
    if here.exists():
        os.environ.setdefault("ARGOS_PACKAGES_DIR", str(here))


def detect_language(text: str) -> str:
    counters = {"ja": 0, "ko": 0, "zh": 0, "ru": 0, "th": 0, "ar": 0, "en": 0}
    for ch in text:
        o = ord(ch)
        if 0x3040 <= o <= 0x30FF:
            counters["ja"] += 2
        elif 0xAC00 <= o <= 0xD7AF or 0x1100 <= o <= 0x11FF:
            counters["ko"] += 2
        elif 0x4E00 <= o <= 0x9FFF:
            counters["zh"] += 1
        elif 0x0400 <= o <= 0x04FF:
            counters["ru"] += 2
        elif 0x0E00 <= o <= 0x0E7F:
            counters["th"] += 2
        elif 0x0600 <= o <= 0x06FF:
            counters["ar"] += 2
        elif ord(ch) < 128 and ch.isalpha():
            counters["en"] += 1
    best = max(counters, key=lambda k: counters[k])
    return best if counters[best] > 0 else "en"


def build_graph(langs) -> dict:
    graph: dict = {}
    for lang in langs:
        graph.setdefault(lang.code, set())
        for t in getattr(lang, "translations_from", []) or []:
            tgt = getattr(t, "to_lang", None)
            if tgt is not None:
                graph[lang.code].add(tgt.code)
    return graph


def find_path(graph: dict, src: str, dst: str, max_hops: int = 3):
    if src == dst:
        return [src]
    if dst in graph.get(src, ()):
        return [src, dst]
    if "en" in graph.get(src, ()) and dst in graph.get("en", ()):
        return [src, "en", dst]
    from collections import deque
    q = deque([[src]])
    seen = {src}
    while q:
        path = q.popleft()
        if len(path) > max_hops + 1:
            continue
        for nxt in sorted(graph.get(path[-1], ())):
            if nxt == dst:
                return path + [nxt]
            if nxt not in seen:
                seen.add(nxt)
                q.append(path + [nxt])
    return None


def translate_text(tr, text: str, from_code: str, to_code: str):
    """返回 (ok, 译文或错误信息)。"""
    langs = tr.get_installed_languages()
    if not langs:
        return False, "Argos 未安装任何模型"
    if from_code == "auto":
        from_code = detect_language(text)
    if from_code == to_code:
        return True, text

    graph = build_graph(langs)
    path = find_path(graph, from_code, to_code)
    if not path:
        return False, (f"无可用翻译路径: {from_code}->{to_code}，"
                       f"请运行 install_models.py 安装模型")
    cur = text
    for a, b in zip(path, path[1:]):
        src = tr.get_language_from_code(a)
        tgt = tr.get_language_from_code(b)
        if src is None or tgt is None:
            return False, f"语言不存在: {a}->{b}"
        trans = src.get_translation(tgt)
        if trans is None:
            return False, f"模型缺失: {a}->{b}"
        cur = trans.translate(cur)
    return True, cur or ""


def cmd_list(tr) -> int:
    for lang in tr.get_installed_languages():
        for t in getattr(lang, "translations_from", []) or []:
            tgt = getattr(t, "to_lang", None)
            if tgt is not None:
                print(f"{lang.code}-{tgt.code}")
    return 0


def cmd_translate(tr, text: str, from_code: str, to_code: str) -> int:
    ok, result = translate_text(tr, text, from_code, to_code)
    if ok:
        if result:
            print(result)
        return 0
    print(f"[error] {result}", file=sys.stderr)
    return 6


def preload_models(tr) -> int:
    """启动时把所有已装模型真正加载进内存，返回加载的语言对数。

    Argos 的模型是【懒加载】的：首次调用 translate 才把模型读进内存。
    若不在启动时预热，第一次真实翻译要 60 秒以上，客户端 90~180 秒
    超时会把它误杀，然后重启进程又重新加载 —— "加载-超时-杀-重启"
    死循环，翻译永远出不来。启动时宁可慢（客户端 ready 等待 240s），
    ready 之后每次翻译都是秒级。
    """
    n = 0
    try:
        langs = tr.get_installed_languages()
        graph = build_graph(langs)
        for a in sorted(graph):
            for b in sorted(graph[a]):
                try:
                    src = tr.get_language_from_code(a)
                    tgt = tr.get_language_from_code(b)
                    t = src.get_translation(tgt)
                    if t is None:
                        continue
                    t.translate(" ")  # 触发模型加载（空文本开销极小）
                    n += 1
                except Exception:
                    continue
    except Exception:
        pass
    return n


def cmd_serve(tr) -> int:
    """常驻模式：一行 JSON 请求 -> 一行 JSON 响应。

    避免每次翻译都重新启动进程（模型加载要 3~4 秒）。
    响应回带请求 id，调用方用它过滤超时后迟到的过期响应。
    """
    import json
    import time as _time
    out = sys.stdout
    # 关键：先预热全部模型，再发 ready。ready 到达即代表模型已在内存。
    _t0 = _time.time()
    n = preload_models(tr)
    print(f"[worker] 模型预热完成: {n} 个语言对, 耗时 {_time.time() - _t0:.1f}s",
          file=sys.stderr, flush=True)
    out.write(json.dumps({"ok": True, "event": "ready"}) + "\n")
    out.flush()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except Exception:
            out.write(json.dumps({"ok": False, "error": "bad request"}) + "\n")
            out.flush()
            continue
        rid = req.get("id")
        try:
            if req.get("cmd") == "list":
                pairs = []
                for lang in tr.get_installed_languages():
                    for t in getattr(lang, "translations_from", []) or []:
                        tgt = getattr(t, "to_lang", None)
                        if tgt is not None:
                            pairs.append([lang.code, tgt.code])
                resp = {"ok": True, "pairs": pairs}
            else:
                ok, result = translate_text(tr, req.get("text", ""),
                                            req.get("from", "auto"),
                                            req.get("to", "zh"))
                resp = {"ok": ok, "text": result if ok else "",
                        "error": "" if ok else result}
        except Exception as e:
            resp = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        resp["id"] = rid
        out.write(json.dumps(resp, ensure_ascii=False) + "\n")
        out.flush()
    return 0


def main():
    if len(sys.argv) < 2:
        print("usage: offline_translate.py TEXT FROM TO | --list", file=sys.stderr)
        return 2

    setup_model_dir()
    try:
        from argostranslate import translate as tr
        from argostranslate import settings
        settings.chunk_type = settings.ChunkType.MINISBD
    except ImportError as e:
        print(f"[error] argostranslate 未安装: {e}", file=sys.stderr)
        return 3

    try:
        arg = sys.argv[1]
        if arg == "--list":
            return cmd_list(tr)
        if arg == "--serve":
            return cmd_serve(tr)
        if len(sys.argv) < 4:
            print("usage: offline_translate.py TEXT FROM TO | --list | --serve",
                  file=sys.stderr)
            return 2
        return cmd_translate(tr, sys.argv[1], sys.argv[2], sys.argv[3])
    except Exception as e:
        print(f"[error] {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
