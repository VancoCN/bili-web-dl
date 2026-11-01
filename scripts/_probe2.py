"""探针 2：验证 (a) 带完整 cookie 能否过网页 412；(b) 纯 API + wbi 签名能否拿到 DASH。"""
import hashlib
import json
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
URL = "https://www.bilibili.com/video/BV1ugxq6HEfH"
BVID = "BV1ugxq6HEfH"
MIXIN_TAB = [46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
             33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40, 61,
             26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36,
             20, 34, 44, 52]
HEAD = {"User-Agent": UA, "Referer": "https://www.bilibili.com/",
        "Origin": "https://www.bilibili.com"}


def get(url, data=None, cookie=""):
    h = dict(HEAD)
    if cookie:
        h["Cookie"] = cookie
    req = urllib.request.Request(url, headers=h, data=data)
    with urllib.request.urlopen(req, timeout=25) as r:
        return r.status, json.loads(r.read().decode())


def main():
    lines = []

    # ---- (a) cookie 能否过网页 ----
    import uuid
    ck = "; ".join([
        f"buvid3={uuid.uuid4()}infoc",
        f"buvid4={uuid.uuid4()}",
        f"_uuid={uuid.uuid4()}infoc",
        "b_nut=%d" % int(time.time()),
    ])
    with open("/tmp/cookies.txt", "w") as f:
        f.write("# Netscape HTTP Cookie File\n")
        for kv in ck.split("; "):
            k, v = kv.split("=", 1)
            f.write(f".bilibili.com\tTRUE\t/\tFALSE\t1893456000\t{k}\t{v}\n")

    for name, extra in (("C1 cookie", []), ("C2 cookie+impersonate", ["--impersonate", "chrome"])):
        r = subprocess.run([sys.executable, "-m", "yt_dlp", "-J", "--no-warnings", "--no-playlist",
                            "--cookies", "/tmp/cookies.txt", "--user-agent", UA,
                            "--add-header", "Referer:https://www.bilibili.com/"] + extra + [URL],
                           capture_output=True, text=True, encoding="utf-8", errors="replace")
        if r.returncode == 0:
            d = json.loads(r.stdout)
            lines.append(f"- [x] {name} : OK — {d.get('title')} / {len(d.get('formats') or [])} formats")
        else:
            err = [x for x in (r.stderr or "").splitlines() if "ERROR" in x]
            lines.append(f"- [ ] {name} : FAIL — {(err[-1] if err else '')[:180]}")

    # ---- (b) 纯 API + wbi ----
    try:
        st, nav = get("https://api.bilibili.com/x/web-interface/nav", cookie=ck)
        wbi = (nav.get("data") or {}).get("wbi_img") or {}
        img = os.path.basename(wbi.get("img_url", "")).split(".")[0]
        sub = os.path.basename(wbi.get("sub_url", "")).split(".")[0]
        raw = img + sub
        mixin = "".join(raw[i] for i in MIXIN_TAB)[:32]
        lines.append(f"- [api] nav HTTP {st} — keys 长度 {len(img)}/{len(sub)}, mixin ok={len(mixin)==32}")

        st, view = get(f"https://api.bilibili.com/x/web-interface/view?bvid={BVID}", cookie=ck)
        vd = view.get("data") or {}
        cid = vd["pages"][0]["cid"]
        lines.append(f"- [api] view HTTP {st} — {vd.get('title')} / {len(vd.get('pages') or [])}P / cid={cid}")

        p = {"bvid": BVID, "cid": str(cid), "qn": "80", "fnval": "4048",
             "fnver": "0", "fourk": "1", "wts": str(int(time.time()))}
        q = urllib.parse.urlencode({k: "".join(c for c in str(v) if c not in "!'()*")
                                    for k, v in sorted(p.items())})
        p["w_rid"] = hashlib.md5((q + mixin).encode()).hexdigest()
        st, pu = get("https://api.bilibili.com/x/player/wbi/playurl?" + urllib.parse.urlencode(p), cookie=ck)
        pd = pu.get("data") or {}
        dash = pd.get("dash") or {}
        vids = dash.get("video") or []
        auds = dash.get("audio") or []
        lines.append(f"- [api] wbi playurl HTTP {st} code={pu.get('code')} "
                     f"dash视频 {len(vids)} 个 / 音频 {len(auds)} 个 / accept={pd.get('accept_description')}")
        if vids:
            for v in vids[:6]:
                lines.append(f"    · id={v.get('id')} {v.get('width')}x{v.get('height')} "
                             f"codec={v.get('codecs')} bw={v.get('bandwidth')}")
            base = vids[0]["baseUrl"]
            req = urllib.request.Request(base, headers={**HEAD, "Range": "bytes=0-2047"})
            try:
                with urllib.request.urlopen(req, timeout=25) as r:
                    lines.append(f"- [cdn] 视频流 Range 请求 HTTP {r.status} "
                                 f"({r.headers.get('Content-Range')})")
            except Exception as exc:  # noqa: BLE001
                lines.append(f"- [cdn] 视频流 Range 请求失败：{exc}")
        else:
            lines.append(f"- [api] 无 dash，原始返回：{json.dumps(pd, ensure_ascii=False)[:220]}")
    except Exception as exc:  # noqa: BLE001
        lines.append(f"- [api] 异常：{exc}")

    report = "\n".join(lines)
    print(report)
    with open("/tmp/report2.md", "w") as f:
        f.write("probe2 @ %s\n\n%s\n" % (time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime()), report))
    repo = os.environ["REPO"]
    tag = "probe-" + os.environ.get("N", "2")
    subprocess.run(["gh", "release", "delete", tag, "--yes", "--cleanup-tag"],
                   capture_output=True)
    subprocess.run(["gh", "release", "create", tag, "--title", tag, "--notes-file",
                    "/tmp/report2.md"], check=False)


if __name__ == "__main__":
    main()
