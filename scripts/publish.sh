#!/usr/bin/env bash
# 把 out/meta.json 写成 Release 正文，媒体文件作为 Release 资产上传。
# 前端按 tag = dl-<job> 直接取这条 Release 的 body 与 assets。
set -uo pipefail

TAG="dl-${JOB}"

if [ ! -f out/meta.json ]; then
  printf '{"ok":false,"job":"%s","error":"动作未产出结果，请查看运行日志"}' "$JOB" > out/meta.json
fi

echo "发布 $TAG"
gh release delete "$TAG" --yes --cleanup-tag >/dev/null 2>&1 || true
gh release create "$TAG" \
  --title "bili ${JOB}" \
  --notes-file out/meta.json >/dev/null

shopt -s nullglob
files=(out/*.mp3 out/*.mp4 out/*.m4a out/*.webm out/*.mkv)
if [ ${#files[@]} -gt 0 ]; then
  echo "上传 ${#files[@]} 个文件"
  gh release upload "$TAG" "${files[@]}" --clobber
else
  echo "无媒体文件（parse 模式）"
fi

echo "完成：https://github.com/${REPO}/releases/tag/${TAG}"
