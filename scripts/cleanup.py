#!/usr/bin/env python3
"""清理超过 24 小时的临时 Release（tag 前缀 dl-），避免仓库越来越臃肿。"""
import json
import os
import subprocess
import sys
from datetime import datetime, timezone

REPO = os.environ.get("REPO", "")
KEEP_HOURS = int(os.environ.get("KEEP_HOURS", "24"))
PREFIX = "dl-"


def gh(*args):
    return subprocess.run(["gh", *args], capture_output=True, text=True,
                          encoding="utf-8", errors="replace")


def main():
    if not REPO:
        print("缺少 REPO，跳过清理")
        return 0
    r = gh("api", f"repos/{REPO}/releases?per_page=100")
    if r.returncode != 0:
        print(f"读取 Release 失败：{r.stderr.strip()[:200]}")
        return 0
    try:
        releases = json.loads(r.stdout)
    except json.JSONDecodeError:
        return 0

    now = datetime.now(timezone.utc)
    removed = 0
    for rel in releases:
        tag = rel.get("tag_name") or ""
        if not tag.startswith(PREFIX):
            continue
        try:
            created = datetime.fromisoformat(rel["created_at"].replace("Z", "+00:00"))
        except (KeyError, ValueError):
            continue
        age_h = (now - created).total_seconds() / 3600
        if age_h <= KEEP_HOURS:
            continue
        d = gh("release", "delete", tag, "--yes", "--cleanup-tag")
        if d.returncode == 0:
            removed += 1
            print(f"已删除 {tag}（{age_h:.0f}h）")
    print(f"清理完成，共删除 {removed} 条")
    return 0


if __name__ == "__main__":
    sys.exit(main())
