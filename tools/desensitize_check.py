#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
desensitize_check.py — 数据脱敏检查脚本（提交前 / 推送前）

作用：扫描「即将被提交」的暂存文件（git 索引），检查是否含敏感内容。
找到 BLOCK 级敏感项则退出码非 0，用于拦截提交；WARN 级仅提示不拦截。

用法：
    python tools/desensitize_check.py           # 检查暂存区（默认，给 pre-commit 用）
    python tools/desensitize_check.py --all     # 检查全部已跟踪文件（推送前全量扫）
    python tools/desensitize_check.py -v        # 连 WARN 级也打印

退出码：
    0  未发现 BLOCK 级敏感内容（可能有 WARN）
    1  发现 BLOCK 级敏感内容，提交应被拒绝

注意：
    - 本项目提示词 `prompts/*.enc` 是故意保留的加密产物（base64 密文），属预期。
    - 商业授权邮箱 tobias@sina.com 是主动公开的署名，属预期，只作 WARN 例外。
    - 模板里的 xxxxxx / your-... / <your-...> 等占位值会被放行（WARN 提示）。
"""

import os
import re
import sys
import subprocess

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 即使 .gitignore 忽略了，也绝不允许被 -f 强加入库的文件名/目录
BLOCK_FILENAMES = [
    "pipeline_config.yaml",
    "api_config.yaml",
    "local.properties",
    "local.properties.local",
    "01b_amap_cache.json",
    "01b_geo_alias.json",
    "DEPLOY_PUBLIC_HTTPS.md",
    "PROJECT_MEMORY.md",
    "FINAL_IMPLEMENTATION_REPORT.md",
    "IMPLEMENTATION_REPORT.md",
    "IMPLEMENTATION_SUMMARY.md",
    "SUCCESS_REPORT.md",
    "REMOTE_ANALYSIS_DESIGN.md",
    "MOBILE_APP_DEV_GUIDE.md",
]
BLOCK_DIRS = [
    "blog/",
    "test/",
    "docs/",
]
BLOCK_SUFFIXES = [
    ".keystore",
    ".jks",
    ".p12",
    ".pfx",
]
BLOCK_NAME_PREFIX = ["用户操作手册", "运维操作手册"]

# BLOCK 级内容正则（命中即拒绝提交）
BLOCK_PATTERNS = [
    # 常见云端 API key
    (r"sk-[A-Za-z0-9\-_]{12,}", "疑似 OpenAI/兼容 API key (sk-)"),
    (r"ark-[A-Za-z0-9\-_]{12,}", "疑似火山引擎 API key (ark-)"),
    (r"AKID[A-Za-z0-9]{10,}", "疑似腾讯云 SecretId (AKID)"),
    (r"ghp_[A-Za-z0-9]{20,}", "疑似 GitHub Personal Access Token"),
    (r"github_pat_[A-Za-z0-9_]{20,}", "疑似 GitHub fine-grained token"),
    (r"-----BEGIN [ A-Z]*PRIVATE KEY-----", "疑似私钥材料"),
    # 高德 Key（32 位 hex，且带 amap 上下文）
    (r"amap.{0,60}?[\"']?([0-9a-f]{32})[\"']?", "疑似高德 Key (32hex)"),
]

# WARN 级内容正则（仅提示，不拦截）
WARN_PATTERNS = [
    # 私有 IPv4（前 3 段为私有网段，且四段均为数字；含占位 x 的会跳过）
    (r"\b(10|172\.(1[6-9]|2\d|3[01])|192\.168)\.\d{1,3}\.\d{1,3}\.\d{1,3}\b", "疑似私有内网 IPv4 地址"),
    # 邮箱（tobias@sina.com 除外）
    (r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[a-zA-Z]{2,}", "疑似邮箱地址"),
    # 已知个人域名
    (r"tobias-zeng\.cn", "疑似个人域名"),
    # 密码/密钥字段（值非占位符时算 BLOCK，占位符则 WARN；见下方值判定）
    (r"(password|passwd|pwd|secret|auth_token)\s*[:=]\s*[\"']([^\"']{4,})[\"']", "疑似密码/密钥字段"),
    # 长随机串（≥40 位 base64 风格，可能是密钥/token）
    (r"\b[A-Za-z0-9\+/_-]{40,}\b", "疑似长随机字符串"),
]

PLACEHOLDER_HINTS = ("xxx", "your", "<", "example", "placeholder", "todo", "changeme", "passw", "token", "key", "xxxx")


def _is_placeholder(value: str) -> bool:
    v = value.lower()
    return any(h in v for h in PLACEHOLDER_HINTS) or not re.search(r"[0-9]", v) and len(value) < 12
    # 简化的启发式：含占位提示词，或纯字母且很短（多半是示例）


def git_staged_files():
    out = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR"],
        capture_output=True, text=True, cwd=ROOT,
    )
    names = [n for n in out.stdout.splitlines() if n.strip()]
    return names


def git_all_tracked():
    out = subprocess.run(["git", "ls-files"], capture_output=True, text=True, cwd=ROOT)
    return [n for n in out.stdout.splitlines() if n.strip()]


def read_staged(fname):
    """读取索引（暂存）中的文件内容；失败返回 None。"""
    try:
        out = subprocess.run(
            ["git", "show", ":" + fname], capture_output=True, cwd=ROOT,
        )
        if out.returncode != 0:
            return None
        return out.stdout.decode("utf-8", errors="ignore")
    except Exception:
        return None


def check_filename(fname):
    """文件名层面判断。返回 (level, reason) 或 None。"""
    base = os.path.basename(fname)
    path = fname.replace("\\", "/")
    for d in BLOCK_DIRS:
        if path.startswith(d) or ("/" + d) in path:
            # test/、docs/、blog/ 目录整体不应入库
            # 注意 PhotoCleaner/app/src/test 是 Android 单元测试，允许
            if d == "test/" and path.startswith("PhotoCleaner/"):
                continue
            return ("BLOCK", f"目录 {d} 不应纳入版本库（本地保留）")
    for f in BLOCK_FILENAMES:
        if fname == f:
            return ("BLOCK", f"敏感文件 {f} 不应纳入版本库")
    for suf in BLOCK_SUFFIXES:
        if base.endswith(suf):
            return ("BLOCK", f"签名/密钥文件 {base} 不应纳入版本库")
    for pre in BLOCK_NAME_PREFIX:
        if base.startswith(pre):
            return ("BLOCK", f"敏感文档 {base} 不应纳入版本库")
    return None


def scan_content(fname, content):
    """内容层面判断。返回 (level, reason, matched_text) 列表。"""
    findings = []
    for pat, desc in BLOCK_PATTERNS:
        for m in re.finditer(pat, content, re.IGNORECASE):
            findings.append(("BLOCK", desc, m.group(0)[:60]))
    for pat, desc in WARN_PATTERNS:
        for m in re.finditer(pat, content, re.IGNORECASE):
            matched = m.group(0)[:60]
            if desc == "疑似私有内网 IPv4 地址":
                # 跳过占位 x.x.x.x（四段非全数字）
                if "x" in matched or "X" in matched:
                    continue
            if desc == "疑似邮箱地址":
                if "tobias@sina.com" in matched:
                    continue  # 主动公开的授权邮箱，预期
                # 无真实域名的占位邮箱
                if any(h in matched for h in ("example.com", "example.org", "your-")):
                    continue
            if desc == "疑似密码/密钥字段":
                val = m.group(2)
                if _is_placeholder(val):
                    # 占位符：WARN 提示，不拦截
                    findings.append(("WARN", "密码/密钥字段为占位值（确认后放行）", matched))
                else:
                    findings.append(("BLOCK", "疑似真实密码/密钥", matched))
                continue
            if desc == "疑似长随机字符串":
                # 跳过 .enc 密文（故意保留的加密提示词）与占位
                if fname.endswith(".enc"):
                    continue
                if _is_placeholder(matched):
                    continue
                findings.append(("WARN", "疑似长随机字符串(可能是密钥/哈希，请核对)", matched))
                continue
            findings.append(("WARN", desc, matched))
    return findings


def main():
    args = sys.argv[1:]
    scan_all = "--all" in args or "-a" in args
    verbose = "-v" in args

    files = git_all_tracked() if scan_all else git_staged_files()
    if not files:
        print("[desensitize] 没有待检查的文件。")
        return 0

    block_hits, warn_hits = [], []
    for fname in files:
        fn = check_filename(fname)
        if fn:
            block_hits.append((fname, "文件名", fn[1], ""))
            continue
        content = read_staged(fname)
        if content is None:
            continue
        for level, desc, matched in scan_content(fname, content):
            if level == "BLOCK":
                block_hits.append((fname, "内容", desc, matched))
            else:
                warn_hits.append((fname, "内容", desc, matched))

    print("=" * 72)
    print("数据脱敏检查 / Desensitization check")
    print("=" * 72)
    if block_hits:
        print(f"\n[BLOCK / 拦截] 发现 {len(block_hits)} 处敏感项，提交被拒绝：\n")
        for fname, where, desc, matched in block_hits:
            print(f"  ✗ {fname}  [{where}] {desc}")
            if matched:
                print(f"      → 匹配: {matched}")
        print("\n  请先移除这些内容再提交。确实已确认安全时，可用 git commit --no-verify 强制提交（不推荐）。")
    else:
        print("\n[BLOCK] 无敏感级内容，本次检查通过。")

    if warn_hits and (verbose or not block_hits):
        print(f"\n[WARN / 提示] 发现 {len(warn_hits)} 处需人工确认（不拦截）：\n")
        for fname, where, desc, matched in warn_hits:
            print(f"  ! {fname}  [{where}] {desc}")
            if matched:
                print(f"      → 匹配: {matched}")

    print("\n" + "=" * 72)
    return 1 if block_hits else 0


if __name__ == "__main__":
    sys.exit(main())
