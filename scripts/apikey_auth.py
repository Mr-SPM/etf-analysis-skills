#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
API Key 配置工具 —— 三种方式, 挑最省事的用。

走势数据接口需要 API Key 认证 (Authorization: Bearer etf_xxxxx...)。

方式 1 (最简单, 推荐): 直接运行, 按提示粘贴
    python apikey_auth.py
    → 脚本打印获取地址, 你把 Key 粘贴进来回车即可

方式 2: 直接跟在命令后面
    python apikey_auth.py etf_xxxxx
    python apikey_auth.py --key etf_xxxxx      # 等价写法

方式 3: 环境变量 (适合 CI / 脚本, 不落盘)
    export ETF_API_KEY=etf_xxxxx               # Linux/macOS
    setx ETF_API_KEY "etf_xxxxx"               # Windows 永久

从哪里拿 Key
------------
1. 浏览器打开 https://stock2.1168168.xyz/ 并登录
2. 右上角用户菜单 → 「API Key 管理」→ 生成
3. 复制整串 (以 etf_ 开头)

配置好后, fetch_etf.py 会自动读取, 不需要每次传参。

Key 特性
--------
- 存明文, 永久有效 (不像登录 token 那样会过期)
- 可随时在网站上吊销; 吊销后接口返回 401, 重新生成即可

安全提示: Key 存在 ~/.workbuddy-ai/etf-plugin/config.json,
请勿分享该文件, 也不要提交到版本库。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

CONFIG_PATH = Path.home() / ".workbuddy-ai" / "etf-plugin" / "config.json"
APIKEY_PAGE = "https://stock2.1168168.xyz/settings/apikey"
API_KEY_PREFIX = "etf_"
ENV_VAR = "ETF_API_KEY"

# 完整格式: etf_ + 64 位十六进制。用于校验粘贴是否完整。
_KEY_RE = re.compile(r"^etf_[0-9a-fA-F]{64}$")


# --------------------------------------------------------------------------
# 配置读写
# --------------------------------------------------------------------------
def load_config() -> dict:
    if CONFIG_PATH.exists():
        try:
            return json.loads(CONFIG_PATH.read_text(encoding="utf-8")) or {}
        except Exception:
            return {}
    return {}


def save_config(cfg: dict) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def save_api_key(key: str) -> None:
    key = key.strip()
    cfg = load_config()
    cfg["api_key"] = key
    save_config(cfg)
    print()
    print(f"  ✓ 已保存到 {CONFIG_PATH}")
    print(f"    前缀 {key[:12]}...  长度 {len(key)}")
    print("    之后运行 fetch_etf.py 会自动读取, 不用再传 --api-key。")


# --------------------------------------------------------------------------
# Key 规范化: 容忍各种粘贴形态
# --------------------------------------------------------------------------
def normalize_key(raw: str) -> str:
    """把用户粘贴的内容清洗成纯 Key。

    粘贴时常见附带物: 引号、首尾空格、Bearer 前缀、URL 编码等。
    直接 strip() 往往不够, 这里统一处理掉。
    """
    s = (raw or "").strip()
    if not s:
        return ""

    # 去掉包裹的引号。
    # 注意: 英文引号是同字符成对 (""), 中文引号是不同字符 (" "),
    # 所以要分别判断, 不能只比较 s[0] == s[-1]。
    _PAIRS = (
        ('"', '"'),
        ("'", "'"),
        ("\u201c", "\u201d"),  # “ ”
        ("\u2018", "\u2019"),  # ‘ ’
    )
    for left, right in _PAIRS:
        if len(s) >= 2 and s[0] == left and s[-1] == right:
            s = s[1:-1].strip()
            break

    # 去掉 "Bearer xxx" 前缀
    if s.lower().startswith("bearer "):
        s = s[7:].strip()

    # 去掉 URL 编码残留 (如复制自 query string 时)
    s = s.replace("%20", " ").strip()

    # 只保留第一段 (防粘贴了多行)
    s = s.split()[0] if s.split() else s

    return s


def key_problem(key: str) -> str | None:
    """校验 Key, 返回问题描述; 没问题返回 None。"""
    if not key:
        return "内容为空"
    if not key.startswith(API_KEY_PREFIX):
        return f"应以 {API_KEY_PREFIX!r} 开头 (当前以 {key[:8]!r} 开头)"
    if not _KEY_RE.match(key):
        # 前缀对了但长度不符 —— 多半是复制不全
        expected = len(API_KEY_PREFIX) + 64
        return (
            f"长度是 {len(key)}, 应为 {expected} 位 —— 可能没复制完整"
            if len(key) != expected
            else "含非法字符 (应为 0-9 / a-f)"
        )
    return None


def mask(key: str) -> str:
    return f"{key[:12]}...{key[-4:]}" if len(key) > 20 else "***"


# --------------------------------------------------------------------------
# 剪贴板 (可选能力: 尽力而为, 失败就退回手动粘贴)
# --------------------------------------------------------------------------
def read_clipboard() -> str:
    """尝试读取系统剪贴板; 取不到返回空串。

    这是"锦上添花"能力 —— 成功时用户连粘贴都省了, 失败时静默退回手动输入,
    绝不因剪贴板不可用而中断流程。

    编码说明: Windows 下 PowerShell 的 stdout 默认不是 UTF-8
    (中文系统为 GBK/CP936), 用 text=True + 默认编码会抛 UnicodeDecodeError。
    所以统一以 bytes 取回, 再逐个编码尝试解码。
    """
    try:
        if sys.platform == "win32":
            import subprocess

            # 先让 PowerShell 自己按 UTF-8 输出, 失败再退回系统默认编码
            out = subprocess.run(
                [
                    "powershell",
                    "-NoProfile",
                    "-Command",
                    "[Console]::OutputEncoding=[Text.Encoding]::UTF8; Get-Clipboard -Raw",
                ],
                capture_output=True,
                timeout=8,
            )
            if out.returncode == 0:
                return _decode_bytes(out.stdout)
        elif sys.platform == "darwin":
            import subprocess

            out = subprocess.run(["pbpaste"], capture_output=True, timeout=8)
            if out.returncode == 0:
                return _decode_bytes(out.stdout)
        else:
            import subprocess

            for cmd in (
                ["xclip", "-selection", "clipboard", "-o"],
                ["xsel", "--clipboard", "--output"],
            ):
                try:
                    out = subprocess.run(cmd, capture_output=True, timeout=8)
                    if out.returncode == 0:
                        return _decode_bytes(out.stdout)
                except FileNotFoundError:
                    continue
    except Exception:
        pass
    return ""


def _decode_bytes(raw: bytes) -> str:
    """把剪贴板原始字节解码成字符串, 逐个编码尝试。

    Key 本身是纯 ASCII, 所以即便编码猜错也不会损坏内容 ——
    这里只是为了不抛异常。
    """
    if not raw:
        return ""
    for enc in ("utf-8-sig", "utf-8", "gbk", "cp936", "latin-1"):
        try:
            return raw.decode(enc).strip()
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="ignore").strip()


# --------------------------------------------------------------------------
# 交互式配置
# --------------------------------------------------------------------------
def _read_line(prompt: str) -> str | None:
    """读一行输入。到达 EOF (管道结束) 返回 None。

    为什么单独包一层: agent / CI 通过管道调用时 stdin 不是终端,
    input() 会直接抛 EOFError。这里统一转成 None, 让调用方能区分
    "用户没输入" 和 "根本没有输入源"。
    """
    sys.stdout.write(prompt)
    sys.stdout.flush()
    try:
        line = sys.stdin.readline()
    except KeyboardInterrupt:
        return None
    if line == "":  # EOF
        return None
    return line.rstrip("\r\n")


def interactive_setup(allow_clipboard: bool = True) -> int:
    """引导用户完成配置。这是默认入口。

    两种运行环境都支持:
      - 终端交互 (tty): 正常提示 + 等待输入
      - 管道 (agent / CI): 若 stdin 有内容则当作 Key 直接读入,
        否则打印指引后返回非零码, 绝不阻塞等待
    """
    interactive = sys.stdin.isatty()

    print()
    print("=" * 52)
    print("  配置 ETF 走势接口 API Key")
    print("=" * 52)
    print()
    print("  获取方式:")
    print(f"    1. 打开 {APIKEY_PAGE}")
    print("    2. 登录后生成 API Key")
    print("    3. 复制整串 (以 etf_ 开头)")
    print()

    # --- 非交互环境 ---
    if not interactive:
        # stdin 可能有内容 (如 `echo etf_xxx | python apikey_auth.py`)
        piped = _read_line("")
        piped = normalize_key(piped or "")
        if piped:
            problem = key_problem(piped)
            if problem:
                print(f"  ✗ Key 格式有问题: {problem}")
                return 2
            save_api_key(piped)
            return 0

        # 没有 stdin 内容 -> 尝试剪贴板, 再不行就只给指引
        if allow_clipboard:
            clip = normalize_key(read_clipboard())
            if clip and key_problem(clip) is None:
                save_api_key(clip)
                print("  (已从剪贴板自动读取)")
                return 0

        print("  当前不是交互终端, 无法等待输入。请改用以下任一方式:")
        print()
        print("    # 方式 1: 把 Key 直接跟在命令后面")
        print("    python apikey_auth.py etf_xxxxx")
        print()
        print("    # 方式 2: 用管道传入")
        print("    echo etf_xxxxx | python apikey_auth.py")
        print()
        print("    # 方式 3: 设置环境变量")
        print(f"    set ETF_API_KEY=etf_xxxxx        (Windows)")
        print(f"    export {ENV_VAR}=etf_xxxxx       (Linux/macOS)")
        print()
        return 1

    # --- 交互终端 ---
    # 如果剪贴板里正好是 Key, 省掉粘贴这一步
    if allow_clipboard:
        clip = normalize_key(read_clipboard())
        if clip and key_problem(clip) is None:
            print(f"  检测到剪贴板里已有 Key: {mask(clip)}")
            ans = _read_line("  直接使用它吗? [Y/n]: ")
            if ans is None:
                print()
                return 130
            if ans.strip().lower() in ("", "y", "yes"):
                save_api_key(clip)
                return 0
            print()

    raw = _read_line("  粘贴 Key 后回车 (直接回车取消):\n  > ")
    if raw is None:
        print("\n  输入已结束, 已取消。")
        return 1

    key = normalize_key(raw)
    if not key:
        print("  未输入内容, 已取消。")
        return 1

    problem = key_problem(key)
    if problem:
        print(f"\n  ✗ Key 格式有问题: {problem}")
        print(f"    请到 {APIKEY_PAGE} 重新复制完整内容。")
        return 2

    save_api_key(key)
    return 0


def status() -> int:
    cfg = load_config()
    key = (cfg.get("api_key") or "").strip()
    env_key = (os.environ.get(ENV_VAR) or "").strip()

    print()
    if key:
        print(f"  配置文件  已配置  {mask(key)}")
        print(f"            {CONFIG_PATH}")
    else:
        print("  配置文件  未配置")

    if env_key:
        print(f"  环境变量  已配置  {mask(env_key)}  ({ENV_VAR})")
    else:
        print(f"  环境变量  未配置  ({ENV_VAR})")

    print()
    if not key and not env_key:
        print(f"  还没有可用的 Key。运行 python apikey_auth.py 按提示配置,")
        print(f"  或到 {APIKEY_PAGE} 生成后传入。")
        return 1

    eff = key or env_key
    problem = key_problem(eff)
    if problem:
        print(f"  ⚠️ 生效的 Key 格式可疑: {problem}")
        return 2
    print("  可以正常使用。")
    return 0


def clear() -> int:
    cfg = load_config()
    had = bool(cfg.pop("api_key", None))
    save_config(cfg)
    print("  已清除配置文件中保存的 API Key。" if had else "  配置文件里本来就没有 Key。")
    if os.environ.get(ENV_VAR):
        print(f"  注意: 环境变量 {ENV_VAR} 仍然生效, 需要另在系统里清除。")
    return 0


# --------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(
        description="ETF 走势接口 API Key 配置工具",
        epilog=(
            "不带任何参数运行即进入交互式配置 (最简单)。\n"
            f"也可以直接把 Key 跟在命令后面, 或用环境变量 {ENV_VAR}。"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument(
        "key_pos",
        nargs="?",
        metavar="KEY",
        help="API Key (可选位置参数), 形如 etf_xxxxx",
    )
    ap.add_argument("--key", dest="key_opt", help="API Key (等价于位置参数)")
    ap.add_argument("--status", action="store_true", help="查看当前配置状态")
    ap.add_argument("--clear", action="store_true", help="清除已保存的 Key")
    ap.add_argument(
        "--no-clipboard",
        action="store_true",
        help="交互式配置时不读剪贴板",
    )
    ap.add_argument(
        "--check",
        metavar="KEY",
        help="只校验格式, 不保存 (用于排查粘贴问题)",
    )
    args = ap.parse_args()

    # 校验模式
    if args.check is not None:
        key = normalize_key(args.check)
        problem = key_problem(key)
        if problem:
            print(f"  ✗ {problem}")
            return 2
        print(f"  ✓ 格式正确  {mask(key)}")
        return 0

    if args.status:
        return status()

    if args.clear:
        return clear()

    # 显式传入 Key (位置参数或 --key 都行)
    explicit = args.key_opt or args.key_pos
    if explicit is not None:
        key = normalize_key(explicit)
        problem = key_problem(key)
        if problem:
            print(f"  ✗ Key 格式有问题: {problem}", file=sys.stderr)
            print(f"    请到 {APIKEY_PAGE} 重新复制完整内容。", file=sys.stderr)
            return 2
        save_api_key(key)
        return 0

    # 都没给 -> 交互式配置
    return interactive_setup(allow_clipboard=not args.no_clipboard)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n  已取消。")
        sys.exit(130)
    except Exception as exc:
        print(f"\n  配置失败: {exc}", file=sys.stderr)
        sys.exit(1)
