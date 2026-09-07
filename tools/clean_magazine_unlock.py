#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
清理 magazine-unlock 杂志锁屏壁纸文件及其产生的空目录。

这类文件是华为 EMUI 魔法锁屏壁纸，不是用户拍摄的照片，且文件名含 UUID/hex
会导致 stage01a 日期正则误匹配出"合法日期"（如 0458-08-21、9978-09-14），
归档到错误的日期文件夹。本脚本用于清理已生成的错误归档。

用法：
    python clean_magazine_unlock.py [目录]
    python clean_magazine_unlock.py "D:\\XY照片_整理输出"
    python clean_magazine_unlock.py "D:\\XY照片_整理输出" --dry-run

不指定目录默认扫描 D:\\XY照片_整理输出
--dry-run 只列出将删除的文件，不实际删除
"""
import os
import sys
import argparse


def find_magazine_files(root):
    """递归查找所有 magazine-unlock- 开头的图片文件"""
    targets = []
    for dirpath, dirnames, filenames in os.walk(root):
        for fn in filenames:
            if fn.lower().startswith("magazine-unlock-"):
                targets.append(os.path.join(dirpath, fn))
    return targets


def cleanup_empty_dirs(start_dir, root):
    """从 start_dir 向上删除空目录，直到遇到非空目录或到达 root"""
    removed = []
    current = start_dir
    root_abs = os.path.abspath(root)
    while current and os.path.abspath(current) != root_abs:
        if not os.path.isdir(current):
            break
        try:
            entries = os.listdir(current)
        except OSError:
            break
        if entries:
            break
        parent = os.path.dirname(current)
        try:
            os.rmdir(current)
            removed.append(current)
        except OSError:
            break
        current = parent
    return removed


def main():
    parser = argparse.ArgumentParser(
        description="清理 magazine-unlock 杂志锁屏壁纸文件及其空目录"
    )
    parser.add_argument(
        "directory", nargs="?", default=r"D:\XY照片_整理输出",
        help="要清理的目录（默认 D:\\XY照片_整理输出）"
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="只列出将删除的文件，不实际删除"
    )
    args = parser.parse_args()

    root = args.directory
    if not os.path.isdir(root):
        print("错误：目录不存在 - " + root)
        sys.exit(1)

    targets = find_magazine_files(root)
    if not targets:
        print("未找到 magazine-unlock- 开头的文件")
        return

    print("找到 " + str(len(targets)) + " 个 magazine-unlock 文件")
    if args.dry_run:
        print("\n[dry-run] 将删除以下文件：")
        for t in targets:
            print("  " + t)
        return

    deleted_files = 0
    parent_dirs = set()
    for f in targets:
        try:
            parent_dirs.add(os.path.dirname(f))
            os.remove(f)
            deleted_files += 1
            print("  删除文件: " + f)
        except OSError as e:
            print("  [失败] " + f + ": " + str(e))

    deleted_dirs = 0
    # 按路径长度降序，先处理深层目录再处理浅层
    for d in sorted(parent_dirs, key=len, reverse=True):
        removed = cleanup_empty_dirs(d, root)
        for r in removed:
            deleted_dirs += 1
            print("  删除空目录: " + r)

    print("\n完成：删除 " + str(deleted_files) + " 个文件，"
          + str(deleted_dirs) + " 个空目录")


if __name__ == "__main__":
    main()
