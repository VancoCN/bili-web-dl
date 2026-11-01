#!/usr/bin/env python3
"""B 站解析 / 下载，走官方 API（不碰 www.bilibili.com 网页）。

为什么不用 yt-dlp：它在 GitHub Actions 的机房 IP 上抓 www.bilibili.com 会稳定拿到
HTTP 412（B 站按 IP 段风控），换 UA、加 cookie、--impersonate 都无效；
而 api.bilibili.com 在同样的机器上 200 正常。所以这里自己走：
  1. view 接口拿元数据与分 P
  2. wbi 签名的 playurl 接口拿 DASH 流
  3. 直接 HTTP 拉流 + ffmpeg 合并/转码

环境变量：
  MODE     parse | download
  URL_RAW  链接、BV 号或 av 号
  KIND     audio | video              （仅 download）
  QUALITY  目标档位 qn，如 80 = 1080P  （仅 download video）
  JOB      前端生成的短任务 ID
  BILI_COOKIE  可选，浏览器 Cookie 头原文（含 SESSDATA）
输出：
  out/meta.json 始终写出
  out/<job>_<标题>.mp3|mp4  下载产物
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from pathlib import Path

OUT = Path("out")
MODE = os.environ.get("MODE", "parse").strip()
URL_RAW = os.environ.get("URL_RAW", "").strip()
KIND = os.environ.get("KIND", "audio").strip()
QUALITY = os.environ.get("QUALITY", "80").strip()
JOB = re.sub(r"[^0-9a-zA-Z]", "", os.environ.get("JOB", "job"))[:16] or "job"
MAX_BYTES = 1900 * 1024 * 1024

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
REFERER = "https://www.bilibili.com/"

QN_LABEL = {6: "240P", 16: "360P", 32: "480P", 64: "720P", 74: "720P60", 80: "1080P",
            112: "1080P+", 116: "1080P60", 120: "4K", 125: "HDR", 126: "杜比视界", 127: "8K"}
AUDIO_LABEL = {30216: "64k", 30232: "132k", 30280: "192k",
               30250: "杜比全景声", 30251: "Hi-Res"}

# wbi 签名用的固定重排表
MIXIN_TAB = [46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
             33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40, 61,
             26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36,
             20, 34, 44, 52]


def log(msg):
    print(f"[bili] {msg}", flush=True)


def cookie_header():
    raw = (os.environ.get("BILI_COOKIE") or "").strip()
    if not raw:
        return ""
    parts = [p.strip() for p in raw.replace("\n", ";").split(";") if "=" in p]
    return "; ".join(parts)


COOKIE = cookie_header()


def req_headers(extra=None):
    h = {"User-Agent": UA, "Referer": REFERER, "Origin": "https://www.bilibili.com",
         "Accept": "*/*"}
    if COOKIE:
        h["Cookie"] = COOKIE
    if extra:
        h.update(extra)
    return h


def fetch_json(url, timeout=25):
    req = urllib.request.Request(url, headers=req_headers())
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def norm_target(raw):
    """返回 (view 接口的查询串, 分P 序号)。"""
    if not raw:
        raise RuntimeError("链接为空")
    text = raw.strip()
    m = re.search(r"(?i)(BV[0-9A-Za-z]{10})", text)
    if m:
        query = f"bvid={m.group(1)}"
    else:
        m = re.search(r"(?i)av(\d+)", text)
        if not m:
            raise RuntimeError("没识别到视频号，请粘贴 bilibili.com/video/BV… 或 BV 号")
        query = f"aid={m.group(1)}"
    pm = re.search(r"[?&]p=(\d+)", text)
    return query, int(pm.group(1)) if pm else 1


def wbi_mixin():
    """从 nav 接口取 img/sub key 算出 mixin key。未登录也能拿到。"""
    data = fetch_json("https://api.bilibili.com/x/web-interface/nav").get("data") or {}
    wbi = data.get("wbi_img") or {}
    img = os.path.basename(wbi.get("img_url", "")).split(".")[0]
    sub = os.path.basename(wbi.get("sub_url", "")).split(".")[0]
    if len(img) < 16 or len(sub) < 16:
        raise RuntimeError("拿不到 wbi 签名密钥（B 站接口异常）")
    return "".join((img + sub)[i] for i in MIXIN_TAB)[:32]


def signed(params, mixin):
    clean = {k: "".join(c for c in str(v) if c not in "!'()*") for k, v in params.items()}
    clean["wts"] = str(int(time.time()))
    query = urllib.parse.urlencode(sorted(clean.items()))
    rid = hashlib.md5((query + mixin).encode()).hexdigest()
    return query + "&w_rid=" + rid


def fetch_playurl(query, cid, mixin):
    base = dict(q.split("=", 1) for q in query.split("&"))
    params = {**base, "cid": cid, "qn": "127", "fnval": "4048",
              "fnver": "0", "fourk": "1"}
    payload = fetch_json("https://api.bilibili.com/x/player/wbi/playurl?" + signed(params, mixin))
    if payload.get("code") != 0:
        raise RuntimeError(f"取流失败：{payload.get('message') or payload.get('code')}")
    return payload.get("data") or {}


def dash_of(data):
    """playurl 返回的整个 data，真正的流在 data.dash 里。"""
    return (data or {}).get("dash") or {}


def video_tiers(data):
    """按 qn 归类，每个档位只留一个最合适的编码（avc1 优先，浏览器兼容）。"""
    by_qn = {}
    for v in dash_of(data).get("video") or []:
        if not v.get("baseUrl"):
            continue
        by_qn.setdefault(v.get("id") or 0, []).append(v)

    pref = ("avc1", "hvc1", "hev1", "av01")
    tiers = []
    for qn in sorted(by_qn):
        group = by_qn[qn]
        group.sort(key=lambda f: (next((i for i, p in enumerate(pref)
                                       if (f.get("codecs") or "").startswith(p)), 9),
                                 -(f.get("bandwidth") or 0)))
        best = group[0]
        tiers.append({"qn": qn, "label": QN_LABEL.get(qn, f"{qn}P"),
                      "codecs": best.get("codecs"), "width": best.get("width"),
                      "height": best.get("height"), "bandwidth": best.get("bandwidth"),
                      "_all": group})
    return tiers


def pick_audio(data):
    auds = [a for a in (dash_of(data).get("audio") or []) if a.get("baseUrl")]
    if not auds:
        raise RuntimeError("没有可用的音频流")
    auds.sort(key=lambda a: (0 if "mp4a" in (a.get("codecs") or "") else 1,
                             -(a.get("bandwidth") or 0)))
    return auds[0]


def human(n):
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f} {unit}".replace(".0 ", " ")
        n /= 1024
    return f"{n:.1f} GB"


def download(urls, dest, label="流"):
    """依次尝试主地址与备用地址，流式写盘。"""
    last = ""
    candidates = [u for u in urls if u]
    for i, u in enumerate(candidates):
        try:
            req = urllib.request.Request(u, headers=req_headers({"Range": "bytes=0-"}))
            with urllib.request.urlopen(req, timeout=60) as resp:
                total = int(resp.headers.get("Content-Length") or 0)
                done = 0
                step = 0
                with open(dest, "wb") as f:
                    while True:
                        chunk = resp.read(1 << 18)
                        if not chunk:
                            break
                        f.write(chunk)
                        done += len(chunk)
                        if total:
                            pct = done * 100 // total
                            if pct >= step + 20:
                                step = pct
                                log(f"{label} {pct}%（{human(done)}/{human(total)}）")
            size = os.path.getsize(dest)
            if size < 1024:
                raise RuntimeError(f"内容过小（{size} 字节）")
            log(f"{label} 完成：{human(size)}")
            return size
        except Exception as exc:  # noqa: BLE001
            last = f"{type(exc).__name__}: {exc}"
            log(f"{label} 第 {i + 1} 个地址失败（{last}），换备用地址")
    raise RuntimeError(f"{label}下载失败：{last}")


def ffmpeg(*args):
    exe = shutil.which("ffmpeg") or "ffmpeg"
    r = subprocess.run([exe, "-y", "-loglevel", "error", *args],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg 失败：{(r.stderr or '').strip()[:200]}")


def sanitize(name):
    clean = re.sub(r"[\\/:*?\"<>|\s]+", "_", name or "video").strip("._")
    return (clean or "video")[:48]


def collect(view, dash, want_page):
    data = view.get("data") or {}
    if not data:
        raise RuntimeError("视频不存在、已删除或不可见")
    pages = [{"index": p.get("page"), "title": p.get("part") or f"P{p.get('page')}",
              "duration": p.get("duration"), "cid": p.get("cid")}
             for p in (data.get("pages") or [])]
    target = next((p for p in pages if p["index"] == want_page), pages[0] if pages else None)
    if not target:
        raise RuntimeError("这条视频没有可播放的分 P")

    tiers = video_tiers(dash)
    audio = None
    try:
        a = pick_audio(dash)
        audio = {"id": a.get("id"), "label": AUDIO_LABEL.get(a.get("id"), "音频"),
                 "bandwidth": a.get("bandwidth"), "codecs": a.get("codecs")}
    except RuntimeError:
        pass

    cover = data.get("pic") or ""
    if cover.startswith("http://"):
        cover = "https://" + cover[7:]
    stat = data.get("stat") or {}
    meta = {
        "ok": True, "mode": MODE, "job": JOB,
        "id": data.get("bvid") or "", "aid": data.get("aid"),
        "title": data.get("title") or "未命名",
        "uploader": (data.get("owner") or {}).get("name") or "",
        "duration": target["duration"] or data.get("duration") or 0,
        "cover": cover, "desc": (data.get("desc") or "").strip()[:400],
        "view": stat.get("view") or 0, "like": stat.get("like") or 0,
        "qualities": [{"qn": t["qn"], "label": t["label"], "height": t["height"],
                       "codecs": t["codecs"], "bandwidth": t["bandwidth"]} for t in tiers],
        "audio": audio,
        "pages": [{k: p[k] for k in ("index", "title", "duration")} for p in pages],
        "current_page": target["index"],
        "logged_in": bool(COOKIE),
        "file": None, "error": None,
    }
    return meta, target["cid"], tiers, dash


def grab(dash, tiers, meta):
    """下载并产出成片，返回 out 下的文件路径。"""
    want = int(float(QUALITY or 80))
    fits = [t for t in tiers if t["qn"] <= want] or tiers[:1]
    tier = fits[-1]
    stem = f"{JOB}_{sanitize(meta['title'])}"
    tmp = Path(tempfile.mkdtemp(prefix="bili-"))

    if KIND == "audio":
        a = pick_audio(dash)
        src = tmp / "audio.m4s"
        download([a.get("baseUrl"), *(a.get("backupUrl") or [])], src, "音频流")
        dest = OUT / f"{stem}.mp3"
        ffmpeg("-i", str(src), "-vn", "-c:a", "libmp3lame", "-q:a", "0", str(dest))
        shutil.rmtree(tmp, ignore_errors=True)
        return dest

    v = tier["_all"][0]
    src_v = tmp / "video.m4s"
    download([v.get("baseUrl"), *(v.get("backupUrl") or [])], src_v, f"视频流 {tier['label']}")
    a = pick_audio(dash)
    src_a = tmp / "audio.m4s"
    download([a.get("baseUrl"), *(a.get("backupUrl") or [])], src_a, "音频流")

    dest = OUT / f"{stem}.mp4"
    ffmpeg("-i", str(src_v), "-i", str(src_a), "-c", "copy",
           "-movflags", "+faststart", str(dest))
    shutil.rmtree(tmp, ignore_errors=True)
    return dest


def main():
    started = time.time()
    meta = {"ok": False, "mode": MODE, "job": JOB, "error": None}
    try:
        query, want_page = norm_target(URL_RAW)
        log(f"目标：{query} p={want_page}")
        mixin = wbi_mixin()
        view = fetch_json(f"https://api.bilibili.com/x/web-interface/view?{query}")
        if view.get("code") != 0:
            raise RuntimeError(f"视频信息获取失败：{view.get('message') or view.get('code')}")
        data = view.get("data") or {}
        pages = data.get("pages") or []
        cid = next((p["cid"] for p in pages if p.get("page") == want_page),
                   (pages[0]["cid"] if pages else None))
        if not cid:
            raise RuntimeError("找不到对应的分 P")
        dash = fetch_playurl(f"bvid={data.get('bvid')}" if data.get("bvid") else query,
                             cid, mixin)
        meta, cid, tiers, dash = collect(view, dash, want_page)
        log(f"解析成功：{meta['title']} / {meta['uploader']} / "
            f"档位 {[t['label'] for t in tiers]}")

        if MODE == "download":
            f = grab(dash, tiers, meta)
            if f.stat().st_size > MAX_BYTES:
                raise RuntimeError("文件超过 1.9 GB，Actions 传不上去")
            meta["file"] = {"name": f.name, "size": f.stat().st_size,
                            "size_human": human(f.stat().st_size),
                            "ext": f.suffix.lstrip("."), "kind": KIND}
            log(f"产出 {f.name}（{human(f.stat().st_size)}）")
        meta["elapsed"] = round(time.time() - started, 1)
    except Exception as exc:  # noqa: BLE001
        meta.update(ok=False, error=str(exc).strip()[:300],
                    elapsed=round(time.time() - started, 1))
        log(f"失败：{exc}")

    OUT.mkdir(exist_ok=True)
    (OUT / "meta.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
