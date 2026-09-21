#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Clerk 认证辅助工具。

数据源 2 (stock2.1168168.xyz) 使用 Clerk 认证体系:
  - Publishable Key: pk_live_Y2xlcmsuMTE2ODE2OC54eXok  (base64: "clerk.1168168.xyz$")
  - Frontend API:   https://clerk.1168168.xyz
  - 前端在 axios 请求拦截器里为所有 /api/* 请求注入:
        Authorization: Bearer <Clerk session JWT>
    JWT 来自 clerk.session.getToken()

因此要调用 /api/etf/month, 必须提供 Clerk 的 session JWT。

获取 JWT 的两种方法
--------------------
方法 A (最省事, 推荐):
  1. 浏览器打开 https://stock2.1168168.xyz/ 并完成登录
  2. F12 打开控制台, 执行:
         await window.Clerk.session.getToken()
  3. 复制返回的整串 JWT (很长, 以 eyJ 开头)
  4. 交给技能:
         python fetch_etf.py --out data.json --clerk-token "<粘贴JWT>" --save-creds

方法 B (Clerk FAPI 直连, 需要知道密码):
  用邮箱+密码走 Clerk 的 sign-in API 换取 session。

注意: JWT 有效期通常约 60 秒 ~ 1 小时。如果接口返回
"未登录或登录已过期", 重新执行方法 A 取一个新 JWT 即可。

安全提示: token 保存在 ~/.workbuddy-ai/etf-plugin/config.json,
请勿分享该文件, 也不要提交到版本库。
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from pathlib import Path

FAPI = "https://clerk.1168168.xyz"
PUBLISHABLE_KEY = "pk_live_Y2xlcmsuMTE2ODE2OC54eXok"
CONFIG_PATH = Path.home() / ".workbuddy-ai" / "etf-plugin" / "config.json"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126.0.0.0 Safari/537.36"


def _post(path: str, payload: dict) -> dict:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        FAPI + path,
        data=body,
        headers={
            "Content-Type": "application/json",
            "User-Agent": UA,
            "Origin": "https://stock2.1168168.xyz",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=25) as resp:
        return json.loads(resp.read().decode("utf-8"))


def get_token_for_user(email: str, password: str, debug: bool = False) -> str | None:
    """走 Clerk 密码登录拿 session token (方法 B)。"""
    try:
        # 1. 用邮箱找 identifier
        # Clerk 流程: create sign-in attempt with identifier
        attempt = _post(
            "/v1/client/sign_ins",
            {"identifier": email, "strategy": "password"},
        )
        _log(debug, "sign_in attempt created")
        sid = attempt.get("response", {}).get("id") or attempt.get("id")
        if not sid:
            print("无法创建 sign-in attempt:", json.dumps(attempt)[:400], file=sys.stderr)
            return None

        # 2. 提交密码
        result = _post(
            f"/v1/client/sign_ins/{sid}/attempt_first_factor",
            {"strategy": "password", "password": password},
        )
        _log(debug, "first factor attempted")
        sess_id = (
            result.get("response", {}).get("created_session_id")
            or result.get("created_session_id")
        )
        if not sess_id:
            err = result.get("errors") or [{}]
            print(
                "登录失败:", err[0].get("long_message") or json.dumps(result)[:300],
                file=sys.stderr,
            )
            return None

        # 3. 取 session token
        req = urllib.request.Request(
            f"{FAPI}/v1/client/sessions/{sess_id}/tokens",
            headers={"User-Agent": UA},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=25) as resp:
            tok = json.loads(resp.read().decode("utf-8"))
        return tok.get("jwt")
    except Exception as exc:
        print(f"获取 token 失败: {exc}", file=sys.stderr)
        return None


def _log(debug: bool, msg: str) -> None:
    if debug:
        print(f"[debug] {msg}", file=sys.stderr)


def save_token(token: str) -> None:
    cfg = {}
    if CONFIG_PATH.exists():
        try:
            cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception:
            cfg = {}
    cfg["clerk_token"] = token
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"token 已保存到 {CONFIG_PATH}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Clerk 认证辅助")
    ap.add_argument("--token", help="直接保存手工获取的 JWT (方法 A)")
    ap.add_argument("--email", help="Clerk 账号邮箱 (方法 B)")
    ap.add_argument("--password", help="账号密码 (方法 B)")
    ap.add_argument("--debug", action="store_true")
    ap.add_argument("--show-help", action="store_true", help="打印取 token 的操作说明")
    args = ap.parse_args()

    if args.show_help or (not args.token and not args.email):
        print(__doc__)
        return 0

    if args.token:
        save_token(args.token)
        return 0

    if args.email and args.password:
        tok = get_token_for_user(args.email, args.password, args.debug)
        if tok:
            save_token(tok)
            return 0
        return 1

    print("请提供 --token 或 --email/--password", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
