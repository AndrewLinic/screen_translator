#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""把 make_release.py 打好的 zip 发布到 GitHub Releases。

为什么单独写一个脚本（而不是让你手动拖文件）:
    本机 GitHub 走加速器 TLS 中间人，Git Bash 里的 curl 是 schannel 版，
    --cacert 对它无效（会报 CRYPT_E_NO_REVOCATION_CHECK）；GCM 的 OAuth
    刷新又会卡死。这里统一用 Python + OpenSSL + 项目自定义 CA 包，
    token 优先从 Windows 凭据管理器里 GCM 已存的那条读取，不落盘、不进命令行。

用法:
    python publish_release.py --tag v1.0.0                    # 自动找 release/*.zip
    python publish_release.py --tag v1.0.0 --asset release/屏幕翻译-精简版-v1.0.0.zip
    python publish_release.py --tag v1.0.0 --notes-file NOTES.md
    python publish_release.py --tag v1.0.0 --dry-run          # 只看要做什么
    python publish_release.py --tag v1.0.0 --replace          # 同名附件先删后传

token 来源优先级: --token > 环境变量 GITHUB_TOKEN > Windows 凭据管理器
"""
from __future__ import annotations

import argparse
import ctypes
import http.client
import json
import os
import re
import ssl
import subprocess
import sys
import time
import urllib.parse
from ctypes import wintypes
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DEFAULT_REPO = "AndrewLinic/screen_translator"

DEFAULT_NOTES = """## 屏幕实时翻译 v{tag}

Windows 10/11 64 位免安装版，解压后双击 `屏幕翻译.exe` 即可运行。

### 这个包为什么小
**不含离线翻译模型**（那部分有 842MB）。首次启动会自动下载中英双向模型（约 165MB），
下载过程中状态灯会显示进度；下完即可离线可用。日/韩/繁体等语言可用
`install_models.py --core` 追加，或直接把完整模型目录拷进来。

### 功能
- 屏幕取词区实时 OCR（RapidOCR，本地）
- 翻译走**在线优先 + 离线兜底**：在线用百度翻译 API，失败自动切本地 Argos 模型
- 悬浮字幕条，可拖动/穿透，支持多帧投票提高识别稳定性

### 说明
- 仓库只放源码，模型与词典请用 `download_assets.py` 拉取
- 许可证: MIT
"""


# ---------------------------------------------------------------- 凭据 & TLS

class _CREDENTIAL(ctypes.Structure):
    _fields_ = [
        ("Flags", wintypes.DWORD),
        ("Type", wintypes.DWORD),
        ("TargetName", wintypes.LPWSTR),
        ("Comment", wintypes.LPWSTR),
        ("LastWritten", wintypes.FILETIME),
        ("CredentialBlobSize", wintypes.DWORD),
        ("CredentialBlob", ctypes.POINTER(ctypes.c_byte)),
        ("Persist", wintypes.DWORD),
        ("AttributeCount", wintypes.DWORD),
        ("Attributes", ctypes.c_void_p),
        ("TargetAlias", wintypes.LPWSTR),
        ("UserName", wintypes.LPWSTR),
    ]


def _cred_read(target: str) -> str | None:
    """读 Windows 凭据管理器里某条目，返回明文。找不到返回 None。"""
    if sys.platform != "win32":
        return None
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    p = ctypes.POINTER(_CREDENTIAL)()
    if not advapi32.CredReadW(target, 1, 0, ctypes.byref(p)):
        return None
    c = p.contents
    raw = ctypes.string_at(c.CredentialBlob, c.CredentialBlobSize)
    advapi32.CredFree(p)
    for enc in ("utf-16-le", "utf-8"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return None


def _git(*args: str) -> str:
    try:
        return subprocess.run(["git", *args], cwd=ROOT, capture_output=True,
                              text=True, timeout=15).stdout.strip()
    except Exception:
        return ""


def token_from_credman() -> str | None:
    """按 GCM 的存储习惯挨个试。GCM 有时存在带账号的 target 里（本机就是）。"""
    user = _git("config", "--get", "user.name") or "AndrewLinic"
    for target in ("git:https://github.com",
                   f"git:https://{user}@github.com",
                   "git:https://github.com/"):
        v = _cred_read(target)
        if v:
            return v
    return None


def ca_bundle() -> str | None:
    """找项目用的自定义 CA 包（绕加速器中间人）。"""
    cands = [
        os.environ.get("GITHUB_CA_BUNDLE"),
        _git("config", "--get", "http.https://github.com/.sslCAInfo"),
        _git("config", "--get", "http.sslCAInfo"),
        str(Path.home() / ".workbuddy" / "certs" / "ca-bundle-custom.crt"),
    ]
    for c in cands:
        if c and Path(c).exists():
            return c
    return None


def ssl_ctx() -> ssl.SSLContext:
    cafile = ca_bundle()
    if cafile:
        return ssl.create_default_context(cafile=cafile)
    print("[警告] 未找到自定义 CA 包，若报证书错误请设置 GITHUB_CA_BUNDLE")
    return ssl.create_default_context()


# ---------------------------------------------------------------- HTTP

class Api:
    def __init__(self, token: str, ctx: ssl.SSLContext, timeout: int = 60):
        self.token = token
        self.ctx = ctx
        self.timeout = timeout

    def _conn(self, host: str) -> http.client.HTTPSConnection:
        return http.client.HTTPSConnection(host, timeout=self.timeout, context=self.ctx)

    def request(self, method: str, url: str, body=None, headers: dict | None = None,
                want_json: bool = True):
        u = urllib.parse.urlsplit(url)
        hdrs = {
            "Authorization": f"token {self.token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "screen-translator-release",
        }
        hdrs.update(headers or {})
        conn = self._conn(u.netloc)
        try:
            path = u.path + (("?" + u.query) if u.query else "")
            conn.request(method, path, body=body, headers=hdrs)
            resp = conn.getresponse()
            data = resp.read()
            if resp.status >= 400:
                msg = data.decode("utf-8", "replace")[:600]
                raise RuntimeError(f"HTTP {resp.status} {resp.reason}\n{msg}")
            return json.loads(data) if (want_json and data) else data
        finally:
            conn.close()


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"


# GitHub 会把附件名里的非 ASCII 字符清洗成 "-"（实测 屏幕翻译-精简版-v1.0.0.zip
# 会变成 "-.-v1.0.0.zip"），所以上传前先转成英文名。
NAME_MAP = {"屏幕翻译": "screen-translator", "精简版": "slim", "完整版": "full"}
ASCII_BAD = re.compile(r"[^A-Za-z0-9._-]+")


def ascii_asset_name(name: str) -> str:
    out = name
    for zh, en in NAME_MAP.items():
        out = out.replace(zh, en)
    return ASCII_BAD.sub("-", out).strip("-") or "asset.zip"


def get_release(api: Api, repo: str, tag: str) -> dict | None:
    """拿指定 tag 的 release；不存在返回 None。"""
    u = urllib.parse.urlsplit(f"https://api.github.com/repos/{repo}/releases/tags/{tag}")
    conn = api._conn(u.netloc)
    try:
        conn.request("GET", u.path, headers={
            "Authorization": f"token {api.token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "screen-translator-release",
        })
        resp = conn.getresponse()
        data = resp.read()
        return json.loads(data) if resp.status == 200 else None
    finally:
        conn.close()


def upload_asset(api: Api, upload_url: str, path: Path, name: str) -> None:
    """上传附件。upload_url 形如 https://uploads.github.com/.../assets{?name}"""
    base = upload_url.split("{")[0]
    q = urllib.parse.urlencode({"name": name})
    url = f"{base}?{q}"
    size = path.stat().st_size
    u = urllib.parse.urlsplit(url)
    conn = api._conn(u.netloc)
    headers = {
        "Authorization": f"token {api.token}",
        "Content-Type": "application/zip",
        "Content-Length": str(size),
        "User-Agent": "screen-translator-release",
    }
    print(f"上传 {path.name} -> {name} ({human(size)}) ...")
    t0 = time.time()
    try:
        with open(path, "rb") as fh:
            conn.request("POST", u.path + "?" + u.query, body=fh, headers=headers)
            resp = conn.getresponse()
            data = resp.read()
    finally:
        conn.close()
    if resp.status >= 400:
        raise RuntimeError(f"HTTP {resp.status} {resp.reason}\n{data.decode('utf-8','replace')[:600]}")
    a = json.loads(data)
    dt = max(1e-6, time.time() - t0)
    print(f"  完成: {a.get('browser_download_url')}")
    print(f"  耗时 {dt:.0f}s（{human(size/dt)}/s）")
    if a.get("name") != name:
        print(f"  [注意] 服务端把附件名改成了 {a.get('name')!r}")


def find_asset(explicit: str | None, tag: str) -> Path:
    if explicit:
        p = Path(explicit)
        if not p.is_absolute():
            p = ROOT / p
        if not p.exists():
            raise SystemExit(f"[错误] 附件不存在: {p}")
        return p
    rel = ROOT / "release"
    zips = sorted(rel.glob("*.zip")) if rel.is_dir() else []
    if not zips:
        raise SystemExit("[错误] release/ 下没有 zip，先运行 python make_release.py --tag <版本>")
    if tag:
        hit = [z for z in zips if tag in z.name]
        if hit:
            return hit[0]
    return zips[-1]


def main() -> int:
    ap = argparse.ArgumentParser(description="发布到 GitHub Releases")
    ap.add_argument("--tag", required=True, help="版本号，如 v1.0.0（不存在会自动建 tag）")
    ap.add_argument("--name", default="", help="Release 标题（默认用 tag）")
    ap.add_argument("--asset", default=None, help="要上传的 zip（默认 release/ 下最新）")
    ap.add_argument("--asset-name", default=None,
                    help="附件在 GitHub 上的名字（默认自动转成英文，避免被清洗成 '-.-'）")
    ap.add_argument("--repo", default=DEFAULT_REPO, help=f"owner/repo（默认 {DEFAULT_REPO}）")
    ap.add_argument("--notes", default="", help="Release 说明文字")
    ap.add_argument("--notes-file", default=None, help="从文件读 Release 说明")
    ap.add_argument("--target", default="main", help="tag 指向的分支/提交")
    ap.add_argument("--token", default=None, help="GitHub token（默认自动获取）")
    ap.add_argument("--replace", action="store_true", help="同名附件先删除再上传")
    ap.add_argument("--draft", action="store_true", help="存为草稿")
    ap.add_argument("--prerelease", action="store_true", help="标记为预发布")
    ap.add_argument("--dry-run", action="store_true", help="只打印计划，不调 API")
    args = ap.parse_args()

    asset = find_asset(args.asset, args.tag)
    asset_name = args.asset_name or ascii_asset_name(asset.name)
    notes = args.notes
    if args.notes_file:
        notes = (ROOT / args.notes_file).read_text(encoding="utf-8")
    if not notes:
        notes = DEFAULT_NOTES.format(tag=args.tag.lstrip("v"))

    print(f"仓库    : {args.repo}")
    print(f"tag     : {args.tag}  (target={args.target})")
    print(f"标题    : {args.name or args.tag}")
    print(f"本地文件: {asset.name}  ({human(asset.stat().st_size)})")
    print(f"附件名  : {asset_name}")
    print(f"草稿    : {args.draft}   预发布: {args.prerelease}   替换附件: {args.replace}")
    print(f"CA 包   : {ca_bundle() or '(系统默认)'}")

    if args.dry_run:
        print("\n--dry-run: 未调用 GitHub API")
        print("---- 说明预览 ----")
        print(notes[:800])
        return 0

    token = args.token or os.environ.get("GITHUB_TOKEN") or token_from_credman()
    if not token:
        print("[错误] 拿不到 token：可用 --token 或设环境变量 GITHUB_TOKEN")
        return 1
    print(f"token   : {token[:4]}***（{len(token)} 字符，来自 "
          f"{'参数' if args.token else '环境变量' if os.environ.get('GITHUB_TOKEN') else '凭据管理器'}）")

    api = Api(token, ssl_ctx())

    me = api.request("GET", "https://api.github.com/user")
    print(f"身份校验: {me['login']}")

    # 已有同 tag 的 release 就复用，避免重复建
    existing = get_release(api, args.repo, args.tag)

    if existing:
        rel = existing
        print(f"Release {args.tag} 已存在，复用 (id={rel['id']})")
    else:
        payload = json.dumps({
            "tag_name": args.tag,
            "target_commitish": args.target,
            "name": args.name or args.tag,
            "body": notes,
            "draft": args.draft,
            "prerelease": args.prerelease,
        }).encode("utf-8")
        rel = api.request("POST", f"https://api.github.com/repos/{args.repo}/releases",
                          body=payload, headers={"Content-Type": "application/json"})
        print(f"已创建 Release: {rel['html_url']}")

    same = [a for a in rel.get("assets", []) if a["name"] == asset_name]
    if same:
        if not args.replace:
            print(f"[跳过] 附件 {asset_name} 已存在（加 --replace 可覆盖）")
            print(f"\nRelease 页面: {rel['html_url']}")
            return 0
        for a in same:
            api.request("DELETE", f"https://api.github.com/repos/{args.repo}/releases/assets/{a['id']}",
                        want_json=False)
            print(f"已删除旧附件 {a['name']}")

    upload_asset(api, rel["upload_url"], asset, asset_name)
    rel = api.request("GET", f"https://api.github.com/repos/{args.repo}/releases/tags/{args.tag}")
    print("\n发布完成:")
    print(f"  页面: {rel['html_url']}")
    for a in rel.get("assets", []):
        print(f"  附件: {a['name']}  {human(a['size'])}  {a['browser_download_url']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
