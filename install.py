#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""etf-analysis 技能安装脚本。

把本目录安装到 WorkBuddy 的用户级技能目录, 并做一次自检。

用法:
    python install.py              # 安装到 ~/.workbuddy-ai/skills/etf-analysis
    python install.py --dest <dir> # 安装到指定技能根目录
    python install.py --check      # 只自检, 不安装

只依赖标准库, 可在任意 Python 3.9+ 上运行。
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

SKILL_NAME = "etf-analysis"
HERE = Path(__file__).resolve().parent


def default_skill_root() -> Path:
    return Path.home() / ".workbuddy-ai" / "skills"


def check_python() -> bool:
    major, minor = sys.version_info[:2]
    ok = (major, minor) >= (3, 9)
    flag = "OK " if ok else "FAIL"
    print(f"  [{flag}] Python {major}.{minor} (需要 >= 3.9)")
    return ok


def check_network() -> bool:
    """轻量探测两个数据源可达性 (不下载全量数据)。"""
    import urllib.error
    import urllib.request

    targets = [
        ("实时行情", "https://my-stock.918968.xyz/all"),
        ("月度走势", "https://stock2.1168168.xyz/api/etf/series?month=2026-01&days=1"),
    ]
    all_ok = True
    for label, url in targets:
        req = urllib.request.Request(url, headers={"User-Agent": "etf-analysis-installer"})
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                ok = resp.status == 200
            print(f"  [{'OK ' if ok else 'FAIL'}] {label} 可达")
            all_ok = all_ok and ok
        except Exception as exc:
            print(f"  [FAIL] {label} 不可达: {exc}")
            all_ok = False
    return all_ok


def check_scripts(src: Path) -> bool:
    """语法编译所有脚本。"""
    ok = True
    for py in sorted((src / "scripts").glob("*.py")):
        r = subprocess.run(
            [sys.executable, "-m", "py_compile", str(py)],
            capture_output=True,
            text=True,
        )
        good = r.returncode == 0
        print(f"  [{'OK ' if good else 'FAIL'}] 语法 {py.name}")
        if not good:
            print(r.stderr.strip())
        ok = ok and good
    return ok


def check_no_hardcoded_paths(src: Path) -> bool:
    """确保没有把某一台机器的绝对路径写死。"""
    bad = []
    for f in list(src.rglob("*.py")) + list(src.rglob("*.md")):
        # 跳过安装器自身: 它的检测规则里就包含这些字面量
        if f.name == "install.py":
            continue
        try:
            text = f.read_text(encoding="utf-8")
        except Exception:
            continue
        for needle in ("C:/Users/", "C:\\Users\\", "/Users/User/", "binaries/python"):
            if needle in text:
                bad.append((f.relative_to(src), needle))
    if bad:
        for f, n in bad:
            print(f"  [FAIL] {f} 含硬编码路径片段: {n}")
        return False
    print("  [OK ] 无硬编码绝对路径")
    return True


def run_self_test(src: Path) -> bool:
    """用公开数据源跑一次真实采集, 确认端到端可用。"""
    import tempfile

    script = src / "scripts" / "fetch_etf.py"
    out = Path(tempfile.gettempdir()) / "etf_install_selftest.json"
    # --refresh-month 确保真的走一次网络, 而不是命中本地缓存
    r = subprocess.run(
        [
            sys.executable, str(script),
            "--out", str(out),
            "--skip-live",
            "--refresh-month",
            "--days", "3",
        ],
        capture_output=True,
        text=True,
    )
    if r.returncode != 0:
        print(f"  [FAIL] 自检运行失败 (exit {r.returncode})")
        print((r.stderr or "")[-500:])
        return False

    try:
        data = json.loads(out.read_text(encoding="utf-8"))
        count = data.get("total", 0)
        month = data.get("month")
        srcs = data.get("sources", {})
        month_via = (srcs.get("month") or {}).get("via")
        print(f"  [{'OK ' if count else 'FAIL'}] 月度数据 {count} 只 (月份 {month}, 来源 {month_via})")
        return bool(count)
    except Exception as exc:
        print(f"  [FAIL] 无法解析自检输出: {exc}")
        return False


def install(src: Path, dest_root: Path) -> Path:
    dest = dest_root / SKILL_NAME
    if dest.exists():
        backup = dest.with_name(dest.name + ".bak")
        if backup.exists():
            shutil.rmtree(backup)
        shutil.move(str(dest), str(backup))
        print(f"  (已存在旧版本 -> 备份到 {backup.name})")

    dest_root.mkdir(parents=True, exist_ok=True)
    shutil.copytree(
        src,
        dest,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".git"),
    )
    # 安装目录里不需要安装器自身
    stray = dest / "install.py"
    if stray.exists():
        stray.unlink()
    return dest


def main() -> int:
    ap = argparse.ArgumentParser(description="安装 etf-analysis 技能")
    ap.add_argument("--dest", default="", help="技能根目录 (默认 ~/.workbuddy-ai/skills)")
    ap.add_argument("--check", action="store_true", help="只自检, 不安装")
    args = ap.parse_args()

    src = HERE
    root = Path(args.dest).expanduser() if args.dest else default_skill_root()

    print("etf-analysis 安装程序")
    print("=" * 46)
    print(f"源目录 : {src}")
    print(f"目标   : {root / SKILL_NAME}")
    print()

    print("[1/4] 环境检查")
    env_ok = check_python()
    if not env_ok:
        print("\nPython 版本过低, 无法继续。请升级到 3.9+。")
        return 1

    print()
    print("[2/4] 源码检查")
    src_ok = check_no_hardcoded_paths(src) and check_scripts(src)

    print()
    print("[3/4] 数据源连通性")
    net_ok = check_network()
    if net_ok:
        run_self_test(src)

    if args.check:
        print()
        print("[4/4] 自检模式, 跳过安装")
        return 0 if (env_ok and src_ok) else 1

    print()
    print("[4/4] 安装")
    try:
        dest = install(src, root)
    except Exception as exc:
        print(f"  [FAIL] 安装失败: {exc}")
        return 1
    print(f"  [OK ] 已安装到 {dest}")

    print()
    print("=" * 46)
    if net_ok:
        print("安装完成。数据源可达, 可以直接使用。")
    else:
        print("安装完成, 但部分数据源当前不可达 ——")
        print("请检查网络或代理设置。脚本运行时也会自动降级重试。")
    print()
    print("使用方式:")
    print(f'  python "{dest / "scripts" / "fetch_etf.py"}" --out etf_data.json')
    print()
    print("在 WorkBuddy 中直接说「用 ETF 分析技能分析我的持仓」即可。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
