# 🍊 黄果下载 · huangguo-down

自动下载黄果短剧视频到指定目录，带 WebUI 管理界面。支持增量同步、失败重试、已有视频扫描识别，SQLite 记录下载状态，生成 Emby / Jellyfin 兼容的目录结构与 NFO 元数据。

**端口约定：前端 WebUI `7731`，后端 API `7732`。**

> 本仓库是 `colin4k/HGXZ` 的 NAS 适配分支。原版只提供了指向已失效命名空间的镜像地址（`ghcr.io/wx2cyj/hgxz`，该账号已 404，CI 也从未成功运行过），且没有为 NAS 场景做任何适配。这里补齐了构建、部署和运维所需的部分。

## 一键部署（绿联 NAS）

整个部署只需要**一个文件**：`docker-compose.yml`。它引用 CI 已构建好的多架构镜像，目录里不需要源码、不需要 Dockerfile。

### 第一步：创建目录

SSH 登录 NAS 后执行（路径可按需改）：

```bash
mkdir -p /volume1/docker/huangguo-down/{config,data,logs}
mkdir -p /volume1/media/黄果
```

> 绿联的共享文件夹一般在 `/volume1`、`/volume2` 下。不确定的话用 `df -h` 或 `ls /volume1` 看一下实际名字。
> 先手动建目录很重要：Docker 自动创建的目录属主是 root，之后 Emby/Jellyfin 可能读不到。

### 第二步：写 `docker-compose.yml`

在 `/volume1/docker/huangguo-down/` 下新建 `docker-compose.yml`，内容如下（**把 `volumes` 里四个宿主机路径改成你的实际路径**）：

```yaml
services:
  hgxz:
    # CI 自动推送的多架构镜像（amd64 / arm64）
    image: ghcr.io/mogvl/huangguo-down:latest
    container_name: huangguo-down
    restart: unless-stopped
    ports:
      # 前端 WebUI
      - "7731:7731"
      # 后端 API（只跑接口，无页面）。不需要外部直连就整段注释掉
      - "7732:7732"
    environment:
      TZ: Asia/Shanghai
      # 每日自动同步时间（HH:MM，24 小时制）
      SCHEDULE: "03:30"
      # 启动时立刻同步一次；设 0 则只在定时点跑
      HGXZ_SYNC_ON_START: "1"
      # 前端 / 后端端口
      HGXZ_PORT: "7731"
      HGXZ_API_PORT: "7732"
      # NAS 共享目录需要宽松权限，否则 Emby/Jellyfin 读不到文件
      UMASK: "000"
      # 可选：WebUI 共享口令，留空则不鉴权（仅建议内网使用）
      HGXZ_WEB_TOKEN: ""
      # 可选：站点需要代理时取消注释，改成局域网里的真实代理地址
      # HTTP_PROXY: "http://192.168.1.10:7890"
      # HTTPS_PROXY: "http://192.168.1.10:7890"
      # NO_PROXY: "127.0.0.1,localhost"
    volumes:
      # ↓↓↓ 把下面四个宿主机路径改成你 NAS 上的真实路径 ↓↓↓
      - /volume1/docker/huangguo-down/config:/config
      - /volume1/docker/huangguo-down/data:/data
      - /volume1/docker/huangguo-down/logs:/logs
      - /volume1/media/黄果:/media
    healthcheck:
      test: ["CMD", "curl", "-fsS", "http://127.0.0.1:7731/healthz"]
      interval: 30s
      timeout: 5s
      retries: 3
      start_period: 30s
    logging:
      driver: json-file
      options:
        max-size: "10m"
        max-file: "3"
```

懒得手写就直接下载：

```bash
cd /volume1/docker/huangguo-down
curl -O https://raw.githubusercontent.com/Mogvl/huangguo-down/main/docker-compose.yml
```

### 第三步：启动

```bash
cd /volume1/docker/huangguo-down
docker compose up -d
```

或者用绿联图形界面：Docker → **项目（Project）** → 新建 → 粘贴上面的 compose 内容 → 部署。

### 第四步：验证

```bash
curl http://127.0.0.1:7731/healthz   # 前端
curl http://127.0.0.1:7732/healthz   # 后端
# {"status":"ok","version":"1.1.0-nas","ready":true}
```

浏览器打开 `http://<NAS的IP>:7731`。

### 常用运维

```bash
docker compose logs -f          # 跟踪日志
docker compose restart          # 重启
docker compose pull && docker compose up -d   # 更新到最新镜像
docker compose down             # 停止并移除容器
```

### 用 deploy.sh（可选，功能等价）

克隆仓库后可以用脚本，它把上面的步骤自动化了：

```bash
git clone git@github.com:Mogvl/huangguo-down.git
cd huangguo-down
./deploy.sh
```

脚本会检查 Docker、建数据目录、判断该构建还是拉镜像、启动并打印地址。其他命令：`logs` / `status` / `restart` / `stop` / `down` / `update` / `clean`。

数据目录想改：`DATA_ROOT=/volume2/xxx MEDIA_ROOT=/volume2/yyy ./deploy.sh`，同时改 `docker-compose.yml` 里对应路径。

### 从源码构建（可选）

```bash
docker compose -f docker-compose.yml -f docker-compose.build.yml up -d --build
```

构建配置放在单独的 `docker-compose.build.yml`，只有显式 `-f` 指定时才生效，所以默认的 compose 文件永远不需要源码。

### 常见报错

| 报错 | 原因 |
|---|---|
| `pull access denied for hgxz` | compose 里镜像名还是旧的 `hgxz:nas`，换成 `ghcr.io/mogvl/huangguo-down:latest` |
| `failed to read dockerfile: open Dockerfile` | 用了带 `build` 段的旧 compose 文件，且目录里没有源码；用上面这份（无 `build` 段） |
| `permission denied` 写 `/media` | 目录属主是 root；先 `mkdir` 再启动，或设 `PUID`/`PGID` |

---

## 端口与架构

单个容器内跑两个监听器，共享同一个状态库和同步锁：

| 端口 | 角色 | 说明 |
|---|---|---|
| **7731** | 前端 | 完整 WebUI，同时提供 `/api/*`（同源，无 CORS 问题） |
| **7732** | 后端 | 纯 API，不带页面，开了 CORS 方便外部脚本或独立前端调用 |

`7732` 不映射也能用——前端会走同源请求。需要给外部脚本调 API 时才暴露它。把 `HGXZ_API_PORT` 设为 `0` 或与前端相同，就只监听一个端口。

---

## 整部合并成一个文件

一部短剧几十上百集，每集一两分钟，分开看很麻烦。开启合并后，程序会把整部剧拼成**一个 mp4**，当作电影交给 Emby。

### 目录变化

```
/media/AI成人短剧/
├── 剧名A (全80集).mp4          ← 合并后的整部剧
├── 剧名A (全80集).nfo          ← movie.nfo，Emby 识别为电影
└── 剧名A (全80集)-poster.jpg   ← 封面
```

合并后**分集文件和原来的剧集目录会被删除**，只留这一个文件。

### 开启方式

`docker-compose.yml` 里已经默认开启：

```yaml
environment:
  HGXZ_MERGE: "1"                    # 1=开启合并，0=关闭
  HGXZ_MERGE_DELETE: "1"             # 1=合并后删除分集（省空间）
  HGXZ_MERGE_ONLY_WHEN_COMPLETE: "0" # 1=只在完结后合并，连载中不合并
```

### 增量合并

连载中的剧集新增集数时，**不会重新下载整部**。程序把已有合并文件当作前缀，只下载新增的几集，然后拼接：

```
[已有合并文件(1-3集)] + [新下载的第4集] + [新下载的第5集] → 全5集.mp4
```

旧的 `全3集.mp4` 和它的 NFO 会自动清理，不会留下重复条目。

### 以前下载的怎么办

老版本是一集一个文件。升级后**不用重新下载**，点一下按钮就行。

打开 WebUI，顶部「🔄 立即同步」左边有一个 **「🎬 合并旧数据」** 按钮。有存量分集时按钮会带上数字，比如 `🎬 合并旧数据 (12)`，表示有 12 部剧待合并。

点它 → 确认弹窗里会说明有多少部剧、多少集 → 开始合并，按钮变成「⏳ 合并中…」→ 完成后数字消失。

也可以用命令行（等价）：

```bash
./deploy.sh merge-dry   # 预览，不改动任何文件
./deploy.sh merge       # 执行
```

它会扫描媒体目录，把每个 `剧名 [huangguo-xxxx]/` 下的分集合成一个 `剧名 (全N集).mp4`，写 NFO、搬封面、删掉分集，和正常同步的结果完全一致。**全程不联网**，站点挂了也能用。

也可以在容器里直接调用：

```bash
docker exec huangguo-down python -m HGXZ.cli --merge-existing
docker exec huangguo-down python -m HGXZ.cli --merge-existing --merge-dry-run   # 预览
docker exec huangguo-down python -m HGXZ.cli --merge-existing --force-merge     # 已有合并文件也重做
docker exec huangguo-down python -m HGXZ.cli --merge-existing --keep-episodes   # 保留分集
```

不填路径时用配置里的媒体根目录，也可以显式指定：`--merge-existing /media/黄果/AI成人短剧`。

几点说明：

- 已经合并过的剧会被自动跳过，重复执行没有副作用
- 旧 `tvshow.nfo` 里的简介、年份、类型会继承到新生成的 `movie.nfo`
- 分集文件已删、但合并文件还在的剧不会被误处理
- 想保留分集就加 `--keep-episodes`，会同时留下电影和剧集两个条目

### 注意

- **合并是不可逆的**：分集删除后如果合并文件损坏，只能重新下载。介意的话把 `HGXZ_MERGE_DELETE` 设为 `0`，保留分集。
- 合并需要 ffmpeg 重新封装，一部 80 集的剧大约占用 1-2 分钟 CPU，期间日志会显示 `merging N file(s)`。
- 合并只在**该剧所有集都下载成功**时进行；有失败集时会跳过，等下次重试成功后再合并。
- 已经下载好但没合并的旧数据，把 `HGXZ_MERGE` 设为 `1` 再跑一次同步即可补齐。

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
| `HGXZ_REQUEST_INTERVAL` | `3` | 请求间隔（秒），站点限速时调大 |
| `HGXZ_TIMEOUT` | `45` | 单次请求超时（秒） |
| `HGXZ_ATTEMPTS` | `2` | 单域名重试次数（应对 TLS 中断） |
| `HGXZ_RETRIES` | `3` | 单集重试次数 |
| `HGXZ_MINIMUM_DURATION` | `3` | 有效视频最短时长（秒） |
| `HGXZ_RECHECK_DAYS` | `7` | 已完结专辑复查间隔（天） |
| `HGXZ_FAILURE_COOLDOWN_HOURS` | `24` | 失败集冷却时间（小时） |
| `HGXZ_EPISODE_TIMEOUT` | `1800` | 单集下载超时（秒） |
| `HGXZ_MERGE` | `0`（compose 里为 `1`） | 是否把整部剧合并成一个文件 |
| `HGXZ_MERGE_DELETE` | `1` | 合并后是否删除分集 |
| `HGXZ_MERGE_ONLY_WHEN_COMPLETE` | `0` | 是否只在剧集完结后合并 |
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
| `SSL: UNEXPECTED_EOF_WHILE_READING` | 网络层拦截或站点限速。程序已内置 2 次重试；持续出现就调大 `HGXZ_REQUEST_INTERVAL`，或给容器配代理 |
| 备用域名 301 跳转 | 旧镜像域名已失效，会跳转到新域名，程序自动跟随。`config.example.json` 里已加入新域名 |
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
