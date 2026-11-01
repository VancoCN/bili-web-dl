# BiliFetch · 网页 + GitHub Actions 的 B 站解析下载

一个纯静态网页（可放 GitHub Pages）＋ 一个 GitHub Actions 工作流，实现：

1. 网页里粘贴 B 站链接
2. 点「解析」拿到标题 / UP 主 / 时长 / 封面 / 分 P / 可用清晰度
3. 按需下载 **音频 MP3** 或 **视频 MP4**

页面本身不解码任何视频流，所有网络抓取、转码都在 Actions runner 上完成，
结果通过 **Release 资产** 回传给页面。

```
浏览器 (index.html)
  │  ① POST /actions/workflows/bili.yml/dispatches  {job, mode, url, ...}
  ▼
GitHub Actions (ubuntu-latest)
  │  ② view 接口拿元数据 / 分P → meta.json
  │  ③ (mode=download) wbi 签名的 playurl 接口拿 DASH → 直连 CDN 拉流
  │     ffmpeg：音频转 mp3，视频流+音频流 -c copy 合并 mp4
  │  ④ gh release create dl-<job> --notes-file meta.json + 上传媒体
  ▼
浏览器
     ⑤ 轮询 GET /releases/tags/dl-<job>
        Release 正文 = meta JSON（前端读它渲染卡片 / 报错）
        Release 资产 = 媒体文件（点「下载」直接拉）
```

用 `job` 这个随机 ID 配对请求与结果，避免并发请求互相串号。

## 为什么不用 yt-dlp（重要）

一开始的实现是 `yt-dlp -J` + `yt-dlp -f ...`，本地跑得很好，**但在 Actions 上必然失败**：

```
[BiliBili] BV1ugxq6HEfH: Unable to download webpage: HTTP Error 412: Precondition Failed
```

我在真实 runner 上做过对照实验，结论很明确：

| 做法 | 结果 |
| --- | --- |
| `yt-dlp -J`（默认） | 412 |
| 加真实 Chrome UA + `Referer` | 412 |
| 再加 `buvid3` 等成套 cookie | 412 |
| `--impersonate chrome`（curl_cffi，伪造 TLS 指纹） | 412 |
| `curl https://www.bilibili.com/video/BV...` | **412** |
| `curl https://api.bilibili.com/x/web-interface/view?...` | **200** |
| `curl https://api.bilibili.com/x/player/wbi/playurl?...` | **200** |

也就是说 B 站按 **IP 段**风控了机房地址：网页抓不了，API 完全不拦。yt-dlp 的第一步
就是抓网页，所以它在 Actions 上没有救。改成直连 API 后一切正常：

1. `x/web-interface/view` → 标题 / UP / 时长 / 分 P / cid
2. `x/web-interface/nav` → wbi 签名密钥，再对 playurl 参数做 `w_rid` 签名
3. `x/player/wbi/playurl` → DASH 的视频流与音频流地址
4. 带 `Referer` / `UA` 直接 HTTP 拉流，CDN 返回 206 正常

## 目录

```
index.html                    单文件前端（无构建、无依赖）
.github/workflows/bili.yml    工作流：解析 / 下载 / 发布 / 清理
scripts/bili.py               走 B 站 API 的解析与下载，产出 out/meta.json
scripts/publish.sh            创建 Release 并把结果挂上去
scripts/cleanup.py            删除 24 小时前的 dl-* Release
```

## 部署

1. 新建一个**公开**仓库（私有仓库的 Release 资产下载需要登录，页面里的链接会 403）。
2. 把本目录全部文件推到 `main`。
3. 仓库 **Settings → Pages**，Source 选 `Deploy from a branch`，分支 `main` / 根目录，保存。
4. 生成 token：GitHub **Settings → Developer settings → Fine-grained tokens → Generate new token**
   - Repository access：只勾这一个仓库
   - Permissions：`Actions: Read and write`、`Contents: Read and write`
   - 有效期建议 30 天
5. 打开 Pages 地址，点右上角「设置」，填 `所有者 / 仓库名 / 分支 / token`，保存。
   （部署在 `*.github.io` 上时前两项会自动填好。）

之后：粘贴链接 → 解析 → 选音频或视频 → 下载。

### 可选：配置 Cookie 解锁更多档位

不登录也能解析和下载，**具体能拿到哪些清晰度完全取决于 B 站接口对这条视频的返回**
（实测未登录时有的视频能给到 1080P，有的只到 480P；60fps、HDR、大会员专享档位基本拿不到）。

想要更稳、更高的档位，在仓库 **Settings → Secrets and variables → Actions** 里加一个
`BILI_COOKIE`，内容是从浏览器复制的完整 Cookie 头（必须包含 `SESSDATA`、`DEDEUSERID`、`bili_jct`）。

工作流会把它写成 `cookies.txt` 交给 yt-dlp；没配置就自动降级为游客模式，不会报错。

> Cookie 等同于账号凭证。只放进 Secrets，别贴进 issue、别提交到仓库。
> 大会员画质还取决于账号本身有没有对应权益。

## 实测与限制

| 项 | 情况 |
| --- | --- |
| 清晰度档位 | 由 B 站接口按视频 / 账号返回，页面展示的就是实际可用值（常见 360P–1080P） |
| 竖屏视频 | B 站档位按**短边**算，代码里按 `min(width, height)` 归类，所以 360x480 显示为 360P |
| 音频 | 独立音频流 64k / 132k / 192k，下载后转 MP3 |
| 从点击到出链接 | 通常 30–90 秒，免费 runner 排队时更久；页面超时设 9 分钟 |
| 单文件上限 | Release 资产 2 GB，脚本在 1.9 GB 主动报错 |
| 结果保留 | `dl-*` Release 24 小时后由工作流自动删除，旧链接会失效 |
| 私有仓库 | 不适用，资产下载要带上 token |
| 番剧 / 付费内容 / 直播 | 拿不到，需要账号权限，会在页面显示 B 站返回的错误 |
| Actions 使用条款 | 官方规定 Actions 不应用于与项目构建、测试、部署无关的用途。自己低频率用问题不大，但**不要拿它当公开的免费下载站**，流量一上来既会被限也会被处理 |

如果要做成真正对外的服务，把 `scripts/bili.py` 拿到 VPS / Cloudflare Containers 上跑，
前端换成普通的后端接口即可，逻辑几乎不用改。

## 为什么不能纯前端

B 站的播放地址接口有两个硬门槛：

- `api.bilibili.com` 不对浏览器页面开放 CORS，网页里 `fetch` 直接被拦；
- 拿到的媒体 CDN 地址校验 `Referer` / `User-Agent`，`<video>` 或 `fetch` 都会 403。

所以「解析」和「取流」必须发生在服务端。Actions 正好提供了带 ffmpeg 的临时 Linux 机器，
这也是这套方案成立的原因。

## 本地调试

```bash
cd bili-web-dl
python -m http.server 8080      # 直接双击 index.html 也行
# 打开 http://127.0.0.1:8080/index.html?demo=1 看演示数据，不会调用 GitHub
```

单独测抓取（需要本机有 yt-dlp 和 ffmpeg）：

```bash
MODE=parse URL_RAW=BV1ugxq6HEfH JOB=test python scripts/bili.py
cat out/meta.json
```

## 免责声明

仅供个人学习、备份自己有权使用的内容。请遵守 B 站用户协议与著作权法，
不要用于二次分发或商业用途。
