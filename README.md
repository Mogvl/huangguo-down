# 黄果短剧解析 · Docker 部署版

前后端分离的短剧解析站，**一键部署**，专为绿联 NAS / 群晖 / 任意 Docker 环境准备。

- 前端 `7731`：nginx 托管静态页，并把 API 请求反代到后端
- 后端 `7732`：Node 运行解析核心，负责 API、封面解密、HLS 代理、整集下载
- 零 npm 依赖，镜像体积小，无需构建步骤

> 内容来自第三方公开站点，仅供技术研究与学习。请遵守当地法律法规与版权规则。

---

## 一、快速开始

镜像已发布到 GHCR，**NAS 上不需要构建、不需要拉源码**，只要有 compose 文件即可。

### 绿联 NAS（UGREEN）

1. 打开 **Docker** → **项目** → **新建项目**
2. 项目名称填 `huangguo`
3. 选择「**Compose**」方式，把下面内容整段粘贴进去
4. 点「部署」，等两个容器变为 running
5. 浏览器访问 `http://你的NAS_IP:7731/`

```yaml
services:
  huangguo-web:
    image: ghcr.io/mogvl/huangguo-down-web:latest
    container_name: huangguo-web
    ports: ["7731:80"]
    environment:
      - TZ=Asia/Shanghai
    restart: unless-stopped
    depends_on:
      - huangguo-api

  # 后端：解析上游站点、解密封面、代理 HLS 分片、处理整集下载
  # 不对外映射端口，只被 huangguo-web 通过容器网络调用
  huangguo-api:
    image: ghcr.io/mogvl/huangguo-down-api:latest
    container_name: huangguo-api
    environment:
      - TZ=Asia/Shanghai
      - PORT=7732
      - HOST=0.0.0.0
      # 静态页由 huangguo-web 提供，这里只跑 API
      - SERVE_STATIC=0
    restart: unless-stopped
```

### 任意 Linux / macOS（命令行）

```bash
git clone git@github.com:Mogvl/huangguo-down.git
cd huangguo-down
./deploy.sh
```

脚本会自动拉取镜像、启动服务，并等到服务真正可用后才报告成功。

### 镜像说明

| 镜像 | 基础镜像 | 说明 |
|---|---|---|
| `ghcr.io/mogvl/huangguo-down-web` | nginx:1.27-alpine | 静态页 + API 反代 |
| `ghcr.io/mogvl/huangguo-down-api` | node:20-alpine | 解析核心 + 下载服务 |

两个镜像都带 `latest` 与 `sha` 双标签，公开可匿名拉取。

---

## 二、端口说明

| 端口 | 服务 | 是否对外 | 用途 |
|---|---|---|---|
| **7731** | 前端 nginx | ✅ 对外 | 浏览器访问入口，静态页 + API 反代 |
| **7732** | 后端 Node | ❌ 仅内网 | 只被 nginx 调用；调试时可临时映射 |

**你只需要记住 7731。** 前端页面里的所有请求都是同源相对路径（`/api/...`），
由 nginx 转发到后端，所以不存在跨域问题，也不需要配置任何 API 地址。

如果确实要直连后端调试，把 `docker-compose.yml` 里 `api` 服务的 `ports` 注释打开：

```yaml
  api:
    # ports:
    #   - "7732:7732"
```

---

## 三、日常运维

```bash
./deploy.sh            # 构建并启动
./deploy.sh logs       # 实时日志
./deploy.sh status     # 查看状态
./deploy.sh restart    # 重启
./deploy.sh stop       # 停止
./deploy.sh update     # 拉取最新镜像并重建
./deploy.sh build      # 从源码本地构建（改代码自测用）
```

或直接用 compose：

```bash
docker compose ps
docker compose logs -f
docker compose down
```

### 健康检查

```bash
curl http://127.0.0.1:7731/healthz
# {"status":"ok","uptime":123,"port":7732}
```

两个容器都带 `HEALTHCHECK`，`web` 会等 `api` 变为 healthy 后才启动。

---

## 四、接口一览

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/` | 服务状态 + 分类枚举 |
| GET | `/api/categories` | 分类、排序、榜单、吃瓜枚举 |
| GET | `/api/all` | 首页聚合（并行拉取） |
| GET | `/api/category/{tid}?page=1&size=50&sort=latest` | 分类列表 |
| GET | `/api/ranks/{type}?page=1&size=50` | 排行榜 `hot`/`recommend`/`potential` |
| GET | `/api/topics` | 专辑列表 |
| GET | `/api/topics?slug={slug}&page=1` | 专辑内视频 |
| GET | `/api/chigua?cate=all&page=1` | 吃瓜 `all`/`remen`/`yuanchuang` |
| GET | `/api/search/{关键词}?page=1` | 搜索 |
| GET | `/api/detail/{id}` | 详情 + 剧集列表 |
| GET | `/api/play?url={页面地址}&ep=1` | 解析播放地址 |
| GET | `/media?url={封面地址}` | 封面解密代理 |
| GET | `/raw?url={m3u8}` | HLS 分片/密钥代理 |
| GET | `/download?url={页面地址}&ep=1` | **整集下载**（返回 `.ts`） |
| GET | `/download?...&probe=1` | 下载探针（返回分片数/时长/文件名） |
| GET | `/healthz` | 健康检查 |

在线文档页：`http://你的NAS_IP:7731/api.html`

---

## 五、下载功能

播放页有「**下载本集**」按钮。也可以直接调接口：

```bash
# 先探一下，不实际传输
curl "http://NAS_IP:7731/download?url=https://huangguoai.com/video/5667/&ep=1&probe=1"

# 下载
curl -OJ "http://NAS_IP:7731/download?url=https://huangguoai.com/video/5667/&ep=1"
```

流程：解析 m3u8 → 4 并发拉分片 → AES-128 解密（去 PKCS#7 填充）→ 顺序拼接 → 流式输出。

实测：57 分片 / 223 MB / 约 5 秒；产物为 188 整数倍，TS 同步字节 100% 正确。

输出是 MPEG-TS，转 MP4：

```bash
ffmpeg -i 东京牛头人_第1集.ts -c copy out.mp4
```

---

## 六、本地开发（不用 Docker）

需要 Node 18+。

```bash
npm start          # 单端口模式：http://127.0.0.1:7732，静态页 + API 同端口
npm run check      # 自检 19 项
```

单端口模式下 `SERVE_STATIC` 默认为开，`node server.js` 就能完整跑起来，
不需要 nginx。这与 Docker 的双端口形态共用同一份 `server.js`。

---

## 七、CI

`.github/workflows/ci.yml` 在 push 到 `main` 时执行四个 job：

| Job | 内容 |
|---|---|
| `lint` | JS 语法、package.json、docker-compose.yml、nginx.conf 校验，**并实起容器验证反代链路** |
| `smoke` | 启动服务跑 19 项自检（真实访问上游站点） |
| `publish` | 构建两个镜像并推送到 GHCR（`latest` + `sha`） |
| `verify-images` | 用 compose 拉取刚推送的镜像启动，验证 7731 可用 |

`verify-images` 是关键一环：它模拟 NAS 上的真实流程（`compose pull` → `up -d`），
确认推上去的镜像**真的能一键部署**，而不是只在构建阶段能跑。

### 为什么 lint 里要起容器

nginx 配置有个隐蔽问题：用 `set $backend` + `proxy_pass $backend` 时，
如果没配 `resolver`，容器内会报 `no resolver defined to resolve api` 并直接 **502**。
这种错误 `nginx -t` 完全查不出来（语法是对的），必须真的发一次请求才会暴露。
所以 lint job 里单独起了一个 api + web 的临时网络做真实反代验证。

---

## 八、目录结构

```
huangguo-down/
├── docker-compose.yml          # 一键部署编排
├── deploy.sh                   # 一键脚本（构建/日志/重启/更新）
├── server.js                   # 后端服务：API + 下载（也支持单端口静态托管）
├── package.json                # 零依赖
├── lib/
│   └── download.js             # m3u8 解析 / AES-128 解密 / 分片拼接
├── worker/
│   └── index.js                # 解析核心（官方 JSON 优先，HTML 兜底）
├── public/                     # 静态页（由 nginx 托管）
│   ├── index.html              # 全部剧集
│   ├── ranks.html              # 排行榜
│   ├── topics.html             # 专辑
│   ├── chigua.html             # 吃瓜
│   ├── api.html                # 接口文档
│   ├── play.html               # 播放页
│   ├── app.js / style.css / play.css
│   └── vendor/hls.min.js       # 内置播放器库
├── docker/
│   ├── api.Dockerfile          # node:20-alpine
│   ├── web.Dockerfile          # nginx:1.27-alpine
│   └── nginx.conf              # 反代 + 静态托管
├── scripts/check.js            # 19 项自检
└── .github/workflows/ci.yml    # CI
```

---

## 九、注意事项

- **必须能访问 `huangguoai.com`**：数据实时抓取，不是本地快照
- **`/download` 单集 100~200 MB**：注意 NAS 存储空间；nginx 已关闭该路径的缓冲，不会先落盘到临时目录
- **`Host` 头必须带端口**：nginx 配置里用的是 `$http_host` 而非 `$host`，否则 `/raw` 重写出的分片地址会丢端口导致播放失败
- **后端地址用 `resolver 127.0.0.11` 动态解析**：Docker 内置 DNS。不能用 `upstream` 块写死服务名，因为 nginx 只在启动时解析一次，后端容器重建换 IP 后会永久 502
- **上游改版会失效**：解析依赖页面结构，源站改版后需更新 `worker/index.js`
- 分片地址带 `auth_key` 签名，有效期约 1~2 小时
