#!/usr/bin/env python3
"""B 站链接解析 / 下载，供 GitHub Actions 调用。

环境变量：
  MODE     parse | download
  URL_RAW  用户粘贴的链接或 BV 号
  KIND     audio | video            （仅 download）
  QUALITY  360 | 480 | 720 | 1080   （仅 download video）
  JOB      前端生成的短任务 ID，用于命名资产
输出：
  out/meta.json      始终写出，含 ok / error，前端直接解析
  out/<job>_<标题>.mp3|mp4   下载产物（仅 download）
"""
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

OUT = Path("out")
MODE = os.environ.get("MODE", "parse").strip()
URL_RAW = os.environ.get("URL_RAW", "").strip()
KIND = os.environ.get("KIND", "audio").strip()
QUALITY = os.environ.get("QUALITY", "720").strip()
JOB = re.sub(r"[^0-9a-zA-Z]", "", os.environ.get("JOB", "job"))[:16] or "job"
COOKIE_FILE = Path("cookies.txt")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
MAX_BYTES = 1900 * 1024 * 1024


def log(msg):
    print(f"[bili] {msg}", flush=True)


def norm_url(raw):
    """把裸 BV/av 号补成完整链接。"""
    if re.fullmatch(r"(?i)bv[0-9a-z]{10}", raw):
        return f"https://www.bilibili.com/video/{raw}"
    if re.fullmatch(r"(?i)av\d+", raw):
        return f"https://www.bilibili.com/video/{raw}"
    m = re.search(r"https?://[^\s\"'<>]+", raw)
    if m:
        return m.group(0)
    raise RuntimeError("没识别到链接，请粘贴 bilibili.com/video/... 或 BV 号")


def ytdlp(args, capture=True):
    cmd = [sys.executable, "-m", "yt_dlp", "--no-warnings", "--no-progress",
           "--socket-timeout", "25", "--retries", "5", "--fragment-retries", "5"]
    if COOKIE_FILE.exists():
        cmd += ["--cookies", str(COOKIE_FILE)]
    return subprocess.run(cmd + args, capture_output=capture, text=True,
                          encoding="utf-8", errors="replace")


def short_err(stderr):
    lines = [ln.strip() for ln in (stderr or "").splitlines() if ln.strip()]
    for ln in reversed(lines):
        if ln.startswith("ERROR") or "Unable to" in ln or "failed" in ln.lower():
            return re.sub(r"^ERROR:\s*", "", ln)
    return lines[-1] if lines else "未知错误"


def probe(url):
    r = ytdlp(["-J", "--no-playlist", url])
    if r.returncode != 0:
        raise RuntimeError(short_err(r.stderr))
    return json.loads(r.stdout)


def bili_api(ref):
    """拿分 P 列表与统计；失败不影响主流程。"""
    try:
        if not ref:
            return {}
        query = f"bvid={ref}" if ref.lower().startswith("bv") else f"aid={ref[2:]}"
        req = urllib.request.Request(
            f"https://api.bilibili.com/x/web-interface/view?{query}",
            headers={"User-Agent": UA, "Referer": "https://www.bilibili.com/"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            payload = json.load(resp)
        if payload.get("code") != 0:
            return {}
        return payload.get("data") or {}
    except Exception as exc:  # noqa: BLE001
        log(f"分P接口不可用：{exc}")
        return {}


def quality_tiers(formats):
    """B 站的清晰度档位是「短边」：横屏 1920x1080 -> 1080P，竖屏 360x480 -> 360P。
    同时记下该档位对应的 height，供 -f 过滤使用。"""
    tiers = {}
    for f in formats:
        w, h = f.get("width"), f.get("height")
        if not w or not h or f.get("vcodec") in (None, "none"):
            continue
        label = min(w, h)
        tiers[label] = max(tiers.get(label, 0), h)
    return [{"label": k, "height": tiers[k]} for k in sorted(tiers)]


def collect(info, api):
    formats = info.get("formats") or []
    qualities = quality_tiers(formats)
    audios = [f for f in formats
              if f.get("vcodec") in (None, "none") and f.get("acodec") not in (None, "none")]
    best_audio = max(audios, key=lambda f: f.get("abr") or 0) if audios else None
    pages = [{
        "index": p.get("page"),
        "title": (p.get("part") or f"P{p.get('page')}"),
        "duration": p.get("duration"),
    } for p in (api.get("pages") or []) if p.get("page")]

    cover = info.get("thumbnail") or api.get("pic") or ""
    if cover.startswith("http://"):
        cover = "https://" + cover[7:]

    stat = api.get("stat") or {}
    return {
        "ok": True,
        "mode": MODE,
        "job": JOB,
        "id": info.get("id") or "",
        "title": info.get("title") or api.get("title") or "未命名",
        "uploader": info.get("uploader") or (api.get("owner") or {}).get("name") or "",
        "duration": info.get("duration") or api.get("duration") or 0,
        "cover": cover,
        "desc": (info.get("description") or api.get("desc") or "").strip()[:400],
        "view": stat.get("view") or 0,
        "like": stat.get("like") or 0,
        "qualities": qualities,
        "max_quality": qualities[-1]["label"] if qualities else 0,
        "audio_abr": (best_audio or {}).get("abr") or 0,
        "pages": pages,
        "webpage": info.get("webpage_url") or "",
        "logged_in": COOKIE_FILE.exists(),
        "file": None,
        "error": None,
    }


def pick_video_format(qualities, want):
    """按用户选的档位换算成该视频自己的 height 上限，优先 avc1（兼容性最好）。"""
    try:
        want = int(float(want))
    except (TypeError, ValueError):
        want = 720
    if not qualities:
        cap = want
    else:
        fits = [q for q in qualities if q["label"] <= want]
        cap = fits[-1]["height"] if fits else qualities[0]["height"]
    return [
        f"bv*[height<={cap}][vcodec^=avc1]+ba/b[height<={cap}]/b",
        f"bv*[height<={cap}]+ba/b[height<={cap}]/b",
        "b",
    ]


def download(url, meta, title):
    OUT.mkdir(exist_ok=True)
    stem = f"{JOB}_{sanitize(title)}"
    tpl = str(OUT / f"{stem}.%(ext)s")

    if KIND == "audio":
        attempts = [["-f", "bestaudio/best", "-x", "--audio-format", "mp3",
                     "--audio-quality", "0", "--embed-metadata"]]
    else:
        attempts = [["-f", f, "--merge-output-format", "mp4"]
                    for f in pick_video_format(meta.get("qualities") or [], QUALITY)]

    last = ""
    for extra in attempts:
        r = ytdlp(extra + ["-o", tpl, url], capture=False)
        produced = [p for p in OUT.iterdir() if p.name.startswith(stem) and p.suffix != ".json"]
        if r.returncode == 0 and produced:
            f = max(produced, key=lambda p: p.stat().st_size)
            if f.stat().st_size > MAX_BYTES:
                raise RuntimeError("文件超过 1.9 GB，Actions 上传不了")
            return f
        last = short_err(getattr(r, "stderr", "") or "")
        log(f"尝试失败（{extra[1][:40]}）：{last}，换下一个格式")
    raise RuntimeError(f"下载失败：{last or '未知原因'}")


def sanitize(name):
    clean = re.sub(r"[\\/:*?\"<>|\s]+", "_", name or "video").strip("._")
    return (clean or "video")[:48]


def human(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f}{unit}" if unit != "B" else f"{int(n)}B"
        n /= 1024


def main():
    started = time.time()
    meta = {"ok": False, "mode": MODE, "job": JOB, "error": None}
    try:
        if not URL_RAW:
            raise RuntimeError("链接为空")
        url = norm_url(URL_RAW)
        log(f"目标：{url}")
        info = probe(url)
        ref = info.get("id") or ""
        meta = collect(info, bili_api(ref))
        log(f"解析成功：{meta['title']} / {meta['uploader']} / "
            f"可用清晰度 {[q['label'] for q in meta['qualities']]}")

        if MODE == "download":
            f = download(url, meta, meta["title"])
            meta["file"] = {"name": f.name, "size": f.stat().st_size,
                            "size_human": human(f.stat().st_size),
                            "ext": f.suffix.lstrip("."), "kind": KIND}
            log(f"已下载 {f.name}（{human(f.stat().st_size)}）")
        meta["elapsed"] = round(time.time() - started, 1)
    except Exception as exc:  # noqa: BLE001
        meta.update(ok=False, error=str(exc).strip()[:300],
                    elapsed=round(time.time() - started, 1))
        log(f"失败：{exc}")

    OUT.mkdir(exist_ok=True)
    (OUT / "meta.json").write_text(json.dumps(meta, ensure_ascii=False),
                                   encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
