#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""etf-analysis 技能安装脚本。

把本目录安装到 WorkBuddy 的用户级技能目录。

安装脚本只关心「能否正确部署」: Python 版本 + 源码完整性。
**不做数据源连通性检查** —— 那是运行时状态, 与安装成败无关, 且会在
代理/防火墙/临时抖动的情况下产生虚假失败信号, 甚至打断安装主流程。
需要体检时用独立的 `--doctor` 子命令。

用法:
    python install.py              # 安装到 ~/.workbuddy-ai/skills/etf-analysis
    python install.py --dest <dir> # 安装到指定技能根目录
    python install.py --check      # 只做部署前检查, 不安装
    python install.py --doctor     # 独立体检 (数据源连通性), 不影响任何状态

只依赖标准库, 可在任意 Python 3.9+ 上运行。
"""

from __future__ import annotations

import argparse
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


def check_scripts(src: Path) -> bool:
    """语法编译所有脚本。"""
    ok = True
    scripts = sorted((src / "scripts").glob("*.py"))
    if not scripts:
        print("  [FAIL] 未找到 scripts/*.py")
        return False
    for py in scripts:
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


def check_required_layout(src: Path) -> bool:
    """确认技能必备文件都在 (缺了装过去也是残缺的)。"""
    required = ["SKILL.md", "prompts/prompt_template.md"]
    ok = True
    for rel in required:
        good = (src / rel).is_file()
        print(f"  [{'OK ' if good else 'FAIL'}] 存在 {rel}")
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


def run_doctor(src: Path) -> int:
    """独立体检: 探测数据源连通性。

    刻意与安装解耦 —— 返回值只用于汇报, 不写任何状态, 不影响安装结果。
    任何异常都在这里被吞掉并降级为提示, 绝不冒泡。
    """
    import urllib.error  # noqa: F401  (异常类型覆盖)
    import urllib.request

    print("etf-analysis 环境体检 (doctor)")
    print("=" * 46)
    print("说明: 连通性受代理/防火墙/对端服务状态影响, 与安装无关。")
    print()

    targets = [
        ("实时行情", "https://my-stock.918968.xyz/all"),
        ("月度走势", "https://stock2.1168168.xyz/api/etf/series?month=2026-01&days=1"),
    ]
    problems = 0
    for label, url in targets:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "etf-analysis-doctor"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                reachable = resp.status == 200
            print(f"  [{'OK ' if reachable else 'FAIL'}] {label} 可达")
            if not reachable:
                problems += 1
        except Exception as exc:
            print(f"  [WARN] {label} 不可达: {exc}")
            problems += 1

    print()
    if problems == 0:
        print("全部数据源可达。")
    else:
        print(f"{problems} 个数据源当前不可达。这不影响使用 ——")
        print("采集脚本运行时会自动降级重试 (month -> series 公开兜底)。")
        print("若持续失败, 请检查代理设置 (HTTPS_PROXY / NO_PROXY)。")
    # doctor 永不返回非零码: 体检结果不是错误
    return 0


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
    ap.add_argument("--check", action="store_true", help="只做部署前检查, 不安装")
    ap.add_argument("--doctor", action="store_true", help="独立体检数据源连通性")
    args = ap.parse_args()

    src = HERE

    # doctor 与安装完全解耦, 先处理并直接返回
    if args.doctor:
        return run_doctor(src)

    root = Path(args.dest).expanduser() if args.dest else default_skill_root()

    print("etf-analysis 安装程序")
    print("=" * 46)
    print(f"源目录 : {src}")
    print(f"目标   : {root / SKILL_NAME}")
    print()

    print("[1/3] 环境检查")
    if not check_python():
        print("\nPython 版本过低, 无法继续。请升级到 3.9+。")
        return 1

    print()
    print("[2/3] 源码检查")
    src_ok = check_required_layout(src) & check_no_hardcoded_paths(src) & check_scripts(src)
    if not src_ok:
        print("\n源码检查未通过, 已中止安装 (避免装出残缺的技能)。")
        return 1

    if args.check:
        print()
        print("[3/3] 检查模式, 跳过安装")
        print("\n部署前检查全部通过。")
        return 0

    print()
    print("[3/3] 安装")
    try:
        dest = install(src, root)
    except Exception as exc:
        print(f"  [FAIL] 安装失败: {exc}")
        return 1
    print(f"  [OK ] 已安装到 {dest}")

    print()
    print("=" * 46)
    print("安装完成。")
    print()
    print("使用方式:")
    print(f'  python "{dest / "scripts" / "fetch_etf.py"}" --out etf_data.json')
    print()
    print("在 WorkBuddy 中直接说「用 ETF 分析技能分析我的持仓」即可。")
    print("如需体检数据源连通性: python install.py --doctor")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n已取消。")
        sys.exit(130)
    except Exception as exc:  # 兜底: 任何意外异常都不该是静默中断
        print(f"\n安装程序异常终止: {exc}", file=sys.stderr)
        sys.exit(1)
