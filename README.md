# 🍊 黄果下载 · huangguo-down

自动下载黄果短剧视频到指定目录，带 WebUI 管理界面。支持增量同步、失败重试、已有视频扫描识别，SQLite 记录下载状态，生成 Emby / Jellyfin 兼容的目录结构与 NFO 元数据。

**端口约定：前端 WebUI `7731`，后端 API `7732`。**

> 本仓库是 `colin4k/HGXZ` 的 NAS 适配分支。原版只提供了指向已失效命名空间的镜像地址（`ghcr.io/wx2cyj/hgxz`，该账号已 404，CI 也从未成功运行过），且没有为 NAS 场景做任何适配。这里补齐了构建、部署和运维所需的部分。

## 一键部署（绿联 NAS）

SSH 进 NAS，在仓库目录执行：

```bash
./deploy.sh
```

脚本会自动：检查 Docker → 创建数据目录 → 构建镜像 → 启动容器 → 打印访问地址。首次构建约 3-8 分钟。

也可以直接用 GHCR 预构建镜像（CI 自动推送，支持 amd64/arm64）：

```bash
./deploy.sh pull && ./deploy.sh start
```

其他命令：`logs` / `status` / `restart` / `stop` / `down` / `update` / `clean`，直接跑 `./deploy.sh` 看帮助。

> 如果报 `pull access denied`：GHCR 包默认私有，需要先 `docker login ghcr.io -u <你的GitHub用户名>`，或在 GitHub 上把 Package 可见性改成 Public。
> 如果报 `failed to read dockerfile`：说明用的是旧版同时带 `build` 段的 compose 文件，重新下载本仓库的 `docker-compose.yml` 即可。

---

## 端口与架构

单个容器内跑两个监听器，共享同一个状态库和同步锁：

| 端口 | 角色 | 说明 |
|---|---|---|
| **7731** | 前端 | 完整 WebUI，同时提供 `/api/*`（同源，无 CORS 问题） |
| **7732** | 后端 | 纯 API，不带页面，开了 CORS 方便外部脚本或独立前端调用 |

`7732` 不映射也能用——前端会走同源请求。需要给外部脚本调 API 时才暴露它。把 `HGXZ_API_PORT` 设为 `0` 或与前端相同，就只监听一个端口。

---

## 相对原版改了什么

| 问题 | 原版 | 本版 |
|---|---|---|
| 镜像构建 | 硬编码 `ghcr.io/wx2cyj/hgxz`，跨命名空间 push 必然 403 | 推送 `ghcr.io/mogvl/huangguo-down`，多架构 + tag/sha 标签 |
| 架构 | 只有 `linux/amd64` | `linux/amd64` + `linux/arm64`（绿联 DXP 系列有 ARM 机型） |
| 首次启动 | 缺 `config.json` 直接崩 | entrypoint 自动从模板生成，不会 crash-loop |
| 配置方式 | 只能改 JSON | 全部参数支持环境变量覆盖，NAS 图形界面可直接填 |
| 健康检查 | 无 | `/healthz` + Docker `HEALTHCHECK` |
| WebUI 鉴权 | 完全开放 | 可选 `HGXZ_WEB_TOKEN`，REST 与 WebSocket 同时生效 |
| 僵尸进程 | PID 1 是 python | `tini` 回收 ffmpeg/openssl 子进程 |
| 文件权限 | 只能靠 `UMASK` | 额外支持 `PUID`/`PGID`（LinuxServer 惯例） |
| 日志目录不可写 | 直接抛异常退出 | 降级为仅内存日志，服务继续运行 |
| 启动即同步 | 强制 | `HGXZ_SYNC_ON_START=0` 可关 |
| 前后端端口 | 只有单个 8099 | 前端 7731 / 后端 7732 分离，后端独立 CORS |
| 一键部署 | 手写 docker run | `./deploy.sh` 子命令覆盖日常运维 |

---

## 绿联 NAS 部署

### 前置

在绿联「应用中心」安装 **Docker** 套件。确认 SSH 可用（控制面板 → 终端机 → 启用 SSH）。

### 方式一：只要 compose 文件（最省事）

不需要克隆整个仓库，只要两个文件：

```bash
mkdir -p /volume1/docker/huangguo-down && cd /volume1/docker/huangguo-down
curl -O https://raw.githubusercontent.com/Mogvl/huangguo-down/main/docker-compose.yml
# 编辑里面的四个宿主机路径，然后：
docker compose up -d
```

`docker-compose.yml` 里**没有 `build` 段**，只引用预构建镜像，所以目录里不需要 Dockerfile 和源码。

### 方式二：克隆仓库 + deploy.sh

```bash
git clone git@github.com:Mogvl/huangguo-down.git
cd huangguo-down
./deploy.sh
```

脚本会自动判断：目录里有 `Dockerfile` 就本地构建，没有就拉预构建镜像。

数据目录默认 `/volume1/docker/huangguo-down`（配置/状态/日志）和 `/volume1/media/黄果`（视频）。想改：

```bash
DATA_ROOT=/volume2/docker/huangguo-down MEDIA_ROOT=/volume2/media/黄果 ./deploy.sh
```

同时把 `docker-compose.yml` 里对应的四个路径改掉。

### 方式三：从源码构建

```bash
docker compose -f docker-compose.yml -f docker-compose.build.yml up -d --build
```

构建相关配置在单独的 `docker-compose.build.yml` 里，只有显式 `-f` 指定时才会生效。

### 方式四：绿联图形界面

绿联的图形界面**只能拉取镜像，不能从源码构建**。所以先拉镜像：

```bash
./deploy.sh pull      # 或 docker pull ghcr.io/mogvl/huangguo-down:latest
```

然后：

1. Docker → **镜像**，确认有 `ghcr.io/mogvl/huangguo-down:latest`
2. **创建容器**，名称 `huangguo-down`，勾选 **开机自启**
3. **端口映射**：`7731→7731`、`7732→7732`（TCP）
4. **存储空间**（容器路径必须完全一致，四个都要加）：

   | 宿主机路径 | 容器路径 | 用途 |
   |---|---|---|
   | `/volume1/docker/huangguo-down/config` | `/config` | `config.json` |
   | `/volume1/docker/huangguo-down/data` | `/data` | SQLite 状态库 |
   | `/volume1/docker/huangguo-down/logs` | `/logs` | 轮转日志 |
   | `/volume1/media/黄果` | `/media` | 下载的视频 |

5. **环境变量**：`TZ=Asia/Shanghai`、`SCHEDULE=03:30`、`UMASK=000`、`HGXZ_PORT=7731`、`HGXZ_API_PORT=7732`
6. **命令** 留空（镜像默认 `--daemon`）

### 验证

```bash
curl http://<NAS的IP>:7731/healthz   # 前端
curl http://<NAS的IP>:7732/healthz   # 后端
# {"status":"ok","version":"1.1.0-nas","ready":true}
```

浏览器打开 `http://<NAS的IP>:7731`。

---

## 环境变量

所有变量都是可选的，不填则使用 `config.json` 里的值。

| 变量 | 默认 | 说明 |
|---|---|---|
| `TZ` | `Asia/Shanghai` | 容器时区，影响定时任务 |
| `SCHEDULE` | `03:30` | 每日同步时间 `HH:MM` |
| `HGXZ_SYNC_ON_START` | `1` | 启动时是否立即同步一次 |
| `UMASK` | `022`（镜像）/ `000`（compose） | 新文件权限掩码 |
| `PUID` / `PGID` | 空 | 以指定 uid/gid 运行（需 root 启动） |
| `HGXZ_PORT` | `7731` | 前端 WebUI 端口 |
| `HGXZ_API_PORT` | `7732` | 后端 API 端口（`0` 或与前端相同 = 单端口） |
| `HGXZ_WEB_TOKEN` | 空 | WebUI 共享口令，空=不鉴权 |
| `HGXZ_MEDIA_ROOT` | `/media` | 视频根目录 |
| `HGXZ_STATE_DB` | `/data/state.sqlite3` | 状态库路径 |
| `HGXZ_LOG_DIR` | `/logs` | 日志目录 |
| `HGXZ_CONFIG` | `/config/config.json` | 配置文件路径 |
| `HGXZ_BASE_URL` | 见模板 | 站点主域名 |
| `HGXZ_BACKUP_URLS` | 见模板 | 备用域名，逗号分隔 |
| `HGXZ_REQUEST_INTERVAL` | `2` | 请求间隔（秒） |
| `HGXZ_TIMEOUT` | `30` | 单次请求超时（秒） |
| `HGXZ_RETRIES` | `3` | 单集重试次数 |
| `HGXZ_MINIMUM_DURATION` | `3` | 有效视频最短时长（秒） |
| `HGXZ_RECHECK_DAYS` | `7` | 已完结专辑复查间隔（天） |
| `HGXZ_FAILURE_COOLDOWN_HOURS` | `24` | 失败集冷却时间（小时） |
| `HGXZ_EPISODE_TIMEOUT` | `1800` | 单集下载超时（秒） |
| `HGXZ_LOG_LEVEL` | `INFO` | 日志级别 |
| `HTTP_PROXY` / `HTTPS_PROXY` / `NO_PROXY` | 空 | 代理，站点被墙时需要 |

> **代理注意**：`NO_PROXY` 不要包含站点域名，否则主站会直连超时。填局域网真实 IP，不能填 `127.0.0.1`（那指向容器自己）。

---

## 命令行用法

```bash
# 常驻模式（容器默认）
docker exec huangguo-down python -m HGXZ.cli --daemon

# 单次同步
docker exec huangguo-down python -m HGXZ.cli

# 只同步某个专辑
docker exec huangguo-down python -m HGXZ.cli --only-id 12345

# 只写元数据，不下载视频
docker exec huangguo-down python -m HGXZ.cli --metadata-only

# 扫描媒体目录，把已有视频注册进状态库
docker exec huangguo-down python -m HGXZ.cli --scan-existing

# 重建损坏的封面
docker exec huangguo-down python -m HGXZ.cli --rebuild-covers

# 限制范围，便于调试
docker exec huangguo-down python -m HGXZ.cli --max-albums 3 --max-episodes 2
```

注意：镜像的 `ENTRYPOINT` 是 entrypoint 脚本，`docker exec` 里要写完整的 `python -m HGXZ.cli`。

---

## 媒体库结构

```
/media/
├── AI成人短剧/
│   ├── 剧名A [huangguo-12345]/
│   │   ├── tvshow.nfo
│   │   ├── poster.jpg
│   │   └── Season 01/
│   │       ├── 剧名A.S01E01.mp4
│   │       ├── 剧名A.S01E01.nfo
│   │       └── ...
└── AI成人漫剧/
```

在 Emby / Jellyfin 里把 `/media` 添加为 **节目（电视节目）** 类型即可。

---

## 排错

| 现象 | 处理 |
|---|---|
| 容器反复重启 | `docker logs hgxz` 看第一行；多半是 `/config` 没写权限，或 `config.json` JSON 语法错误 |
| `/healthz` 不通但容器 running | 检查端口映射，以及 `HGXZ_PORT`/`HGXZ_API_PORT` 是否和映射的容器端口一致 |
| 7731 通、7732 不通 | 后端端口没映射，或 `HGXZ_API_PORT=0` 关掉了；只用 WebUI 的话这不影响使用 |
| `pull access denied for ...` | 镜像名写错（比如残留 `hgxz:nas`），或 GHCR 包是私有的需登录 |
| `failed to read dockerfile: open Dockerfile` | compose 文件里带了 `build` 段但目录里没有源码；用仓库版的 `docker-compose.yml`（不含 build 段） |
| 日志 `SSL: WRONG_VERSION_NUMBER` / `404` | DNS 污染，配 `HTTP_PROXY`/`HTTPS_PROXY`，且 `NO_PROXY` 不含站点域名 |
| 日志 `HTTP 403` | 站点风控，调大 `HGXZ_REQUEST_INTERVAL` |
| Emby 读不到文件 | 加 `UMASK=000`，或设 `PUID`/`PGID` 为 Emby 的运行用户 |
| 某集一直失败 | 可能已下架，24 小时冷却；WebUI 点「重置失败」立即重试 |
| 界面无封面 | 站点封面接口异常时自动回退 og:image，两者都失败会记 `cover via ... failed` |
| ARM 机型构建失败 | 确认 `uname -m` 是 `aarch64`；若是 `armv7l` 需自行换基础镜像 |

---

## 已知限制

- 解析依赖目标站点 DOM，站点改版会导致抓取失效（原版已有多次此类修复）。
- 无单元测试，验证靠端到端跑一遍。
- 封面解密使用了站点硬编码的 AES 密钥，属于绕过技术保护措施，请自行评估合规风险。
- 内容来源为标注成人向的第三方站点，版权状态不明。

## 许可证

仅供个人学习与自用。
