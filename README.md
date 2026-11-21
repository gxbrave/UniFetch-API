# UniFetch-API

**中文** | [English](README.en.md)

把分享文字交给它，它把视频和图片准备好，再给你下载地址。

**安装只需要一份 Docker Compose。所有程序都在同一个镜像里，整个服务只启动一个容器。**

## 怎么安装？

你只需要已经装好的 **Docker Compose**。

1. 把下面内容粘贴到 Docker 管理面板的 Compose 中，或保存为 `compose.yaml`。
2. 在 `API_TOKEN` 的引号里，填上你自己的随机长密码，至少 24 个英文或数字字符。这是你的“钥匙”。
3. 点击部署，或在文件所在的目录运行 `docker compose up -d`。

其他选项可以先不改。Cookie 留空就是不使用账号 Cookie。

```yaml
services:
  api:
    image: ghcr.io/gxbrave/unifetch-api:latest
    restart: unless-stopped
    init: true
    ports:
      - "8099:8000"
    environment:
      # Required: set your own random password, at least 24 characters.
      API_TOKEN: ""
      # Empty = 8000. If changed, use the same port on the right above.
      CONTAINER_PORT: ""
      # Optional platform Cookies. Empty = no account Cookie.
      DOUYIN_COOKIE: ""
      XIAOHONGSHU_COOKIE: ""
      BILIBILI_COOKIE: ""
      X_COOKIE: ""
      # Optional settings. Empty = use the default.
      PUBLIC_BASE_URL: ""
      TRUST_PROXY_HEADERS: ""
      RETENTION_HOURS: ""
      MAX_PENDING_JOBS: ""
      DATA_DIR: /data
    volumes:
      - media-data:/data
    shm_size: 512mb
    stop_grace_period: 5m
    logging:
      driver: json-file
      options:
        max-size: 10m
        max-file: "3"

volumes:
  media-data:
```

镜像中已经装好 Python、Chromium、FFmpeg 和全部项目依赖。支持 **amd64 和 arm64**，Docker 会选择适合机器的版本。

启动后，打开 `http://服务器IP:8099`。接口说明在 `/docs`，健康检查在 `/health`。

## 能下载什么？

| 平台 | 内容 |
| --- | --- |
| 抖音 | 视频、图片、Live 图的配对图片和动态视频 |
| 小红书 | 视频、图片；部分内容需要 Cookie |
| 哔哩哔哩 | 视频；画质取决于登录状态和账号权限 |
| X / Twitter | 视频、动图视频、原图；内置解析失败时自动使用免费备用 API |

它会选择这次请求能拿到的最高画质。平台没有提供的画质，它不能变出来。

## 怎么改设置？

直接修改 Compose 里的值，再重新部署即可。

| 设置 | 留空时 | 用途 |
| --- | --- | --- |
| `API_TOKEN` | 必须填写 | 保护接口的钥匙，至少 24 个字符 |
| `CONTAINER_PORT` | `8000` | 容器内端口；修改后，`ports` 右边的数字也要相同 |
| 四个 `*_COOKIE` | 不使用 | 对应平台的账号登录凭证 |
| `PUBLIC_BASE_URL` | 自动判断 | 通常不用填；需要固定域名和路径时再填 |
| `TRUST_PROXY_HEADERS` | `true` | 使用反代传来的域名和协议信息 |
| `RETENTION_HOURS` | `24` | 文件保留多少小时 |
| `MAX_PENDING_JOBS` | `10` | 最多允许多少个排队或运行中的任务 |

`ports` 中的 `8099:8000` 表示“服务器的 8099 通向容器的 8000”。只想换外面的端口，就改左边的 `8099`。

Cookie 像平台的“登录凭证”。先留空试用，需要登录时再填。X Cookie 需同时包含 `auth_token` 和 `ct0`。Cookie 若含 `$`，在 Compose 中写成 `$$`；它会原样变回一个 `$` 传入容器。X Cookie 不会发给免费备用 API。

下载文件和任务放在 Docker 数据卷中。正常停止、更新会保留数据，到期文件会自动清理。

## 怎么给它链接？

把下面的 `YOUR_TOKEN` 换成你的钥匙，地址换成你的服务器地址：

```bash
curl -X POST 'http://localhost:8099/v1/jobs' \
  -H 'Authorization: Bearer YOUR_TOKEN' \
  -H 'Content-Type: application/json' \
  -d '{"text":"在这里粘贴带有链接的分享文字"}'
```

它会返回 `poll_url`，意思是“来这里看进度”。带上相同的 Token，每 2 秒查看一次：

- `queued` / `running`：还在准备，稍等。
- `succeeded`：准备好了，下载 `media` 中的文件，然后停止等待。
- `failed`：显示 `error.message`，然后停止等待。

下载时完整使用返回的文件地址，不要删掉 `expires` 和 `sig`。下载文件不需要再放 API Token。

## iPhone 快捷指令

顺序是：**取得分享文字 → 提交任务 → 等待结果 → 保存媒体**。

| `kind` | 怎么保存 |
| --- | --- |
| `video` / `image` | 下载 `url`，再用系统“存储到相簿” |
| `live_photo` | 将同一项的 `url` 和 `motion_url` 交给第三方 Live Photo 快捷指令库 |

Live 图的图片和视频已经一一配对。第三方库负责合成为 iPhone Live Photo 并保存；`motion_photo_url` 则用于兼容 Motion Photo 的安卓相册。

[中文快捷指令说明](docs/SHORTCUTS.md) · [English guide](docs/SHORTCUTS.en.md)

## 更新、日志和停止

在 `compose.yaml` 所在的目录运行：

```bash
# 更新
docker compose pull
docker compose up -d

# 看日志
docker compose logs --tail=100 -f

# 停止
docker compose down
```

## 更多说明

[域名反代和 API](docs/DOCKER_API.md) · [开发者：源码和测试](docs/DEVELOPMENT.md)

项目使用 [AGPL-3.0](LICENSE)。修改后通过网络提供服务时，也需要按协议提供对应源码；运行中的 `/source.tar.gz` 已附上源码和许可证，不含 Token、Cookie 或下载文件。

部分逻辑改编自 [YunX-Desktop](https://github.com/tidain/YunX-Desktop)，X 解析参考 [FxEmbed](https://github.com/FxEmbed/FxEmbed)。许可证和来源见 [NOTICE](NOTICE)。请下载你有权保存的内容。
