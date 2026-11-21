# UniFetch-API

**中文** | [English](README.en.md)

把分享链接交给它，它把视频和图片准备好，再给你下载地址。

你可以把它想成一个“小助手”：

1. 你给它一段分享文字。
2. 它找到里面的链接，准备好文件。
3. 你的快捷指令拿走文件，存进相册。

它住在你的服务器上。**只需要一个 Docker 容器。**

## 可以拿到什么？

| 平台 | 可以下载的内容 |
| --- | --- |
| 抖音 | 视频、图片、Live 图的图片和动态视频 |
| 小红书 | 视频、图片；一些内容需要 Cookie |
| 哔哩哔哩 | 视频；画质取决于登录状态和账号权限 |
| X / Twitter | 视频、动图视频、原图；内置解析失败时自动使用免费备用 API |

它会挑选**这次能拿到的最高画质**，不会把低画质文件放大后假装高清。平台没有提供的画质，它也不能变出来。

Live 图会给出配对的图片地址和视频地址。iPhone 需要由你选择的第三方快捷指令库把它们合成 Live Photo；这个后端负责准备文件。

## 三步启动

先准备一台装有 **Git 和 Docker Compose** 的电脑或服务器。

### 1. 把项目拿回来

```bash
git clone https://github.com/gxbrave/UniFetch-API.git
cd UniFetch-API
cp .env.example .env
```

### 2. 填好小纸条

用文本编辑器打开 `.env`。

**只必须修改 `API_TOKEN`**：它像开门的钥匙，请换成你自己的随机长密码，至少 24 个字符。不要直接使用示例里的文字，也不要把这把钥匙给别人。

可以用 `openssl rand -hex 32` 生成一把钥匙，再把结果填进去。

端口和 Cookie 都可以先留空。留空就用默认端口、不带 Cookie。

```dotenv
API_TOKEN=replace-with-your-own-random-password-at-least-24-characters

PORT=
CONTAINER_PORT=

DOUYIN_COOKIE=
XIAOHONGSHU_COOKIE=
BILIBILI_COOKIE=
X_COOKIE=
```

`.env` 里的值会传给 Docker 容器。Cookie 也可以直接在 `compose.yaml` 的 `environment` 中填写。通过 `.env` 修改端口时，Compose 会同时更新端口映射。

### 3. 启动小助手

```bash
docker compose up -d --build
docker compose ps
```

第一次要下载工具和制作镜像，请等一会儿。

在服务器上打开：

- 首页：`http://localhost:8099`
- 接口说明：`http://localhost:8099/docs`
- 检查是否启动：`http://localhost:8099/health`

从另一台设备访问时，把 `localhost` 换成服务器的 IP 或域名。

## 这些格子怎么填？

| 环境变量 | 留空时 | 用来做什么 |
| --- | --- | --- |
| `API_TOKEN` | 必须填写 | 请求接口的钥匙，至少 24 个字符 |
| `PORT` | `8099` | 服务器对外的端口 |
| `CONTAINER_PORT` | `8000` | 容器内部使用的端口 |
| `HOST_BIND` | `0.0.0.0` | 监听的服务器 IP；默认允许所有网卡 |
| `DOUYIN_COOKIE` | 不使用 | 抖音登录凭证 |
| `XIAOHONGSHU_COOKIE` | 不使用 | 小红书登录凭证 |
| `BILIBILI_COOKIE` | 不使用 | B 站登录凭证 |
| `X_COOKIE` | 不使用 | X 登录凭证；使用时需包含 `auth_token` 和 `ct0` |
| `PUBLIC_BASE_URL` | 自动判断 | 想固定下载地址的域名和前缀时再填写 |
| `TRUST_PROXY_HEADERS` | `true` | 根据反代传来的信息生成地址 |
| `RETENTION_HOURS` | `24` | 下载文件保留多少小时 |
| `MAX_PENDING_JOBS` | `10` | 最多允许多少个排队或运行中的任务 |

例如，填写 `PORT=9000` 和 `CONTAINER_PORT=8100` 后，服务器的 `9000` 会通向容器里的 `8100`。只修改其中一个也可以。启动和健康检查都会跟着容器端口变化。

Cookie 像平台的“登录凭证”。公开内容可以先不填；需要登录时，再从自己的浏览器中复制对应平台的 Cookie。Cookie 若包含 `$` 或 `#`，在 `.env` 中用单引号包起来。X Cookie 只发给 X 官方接口，不会发给免费备用 API。

**改完 `.env` 后，重新执行 `docker compose up -d` 才会生效。**

## 怎么让它干活？

### 先交给它一个链接

把下面的 `YOUR_TOKEN` 换成你设置的钥匙，把分享文字换成实际内容：

```bash
curl -X POST 'http://localhost:8099/v1/jobs' \
  -H 'Authorization: Bearer YOUR_TOKEN' \
  -H 'Content-Type: application/json' \
  -d '{"text":"把带有链接的分享文字粘贴在这里"}'
```

它会给你一个 `poll_url`。你可以把它理解成“来这里看进度”的地址。

### 再看它做好了没有

用相同的 `Authorization` 请求头，每隔 2 秒访问一次 `poll_url`。

| `status` | 意思 | 接下来做什么 |
| --- | --- | --- |
| `queued` | 正在排队 | 稍等 |
| `running` | 正在准备 | 稍等 |
| `succeeded` | 准备好了 | 下载 `media` 中的文件，结束等待 |
| `failed` | 这次没成功 | 显示 `error.message`，结束等待 |

文件下载地址已经带有临时通行证，下载时不需要再放 API Token。请原样使用地址，保留 `expires` 和 `sig` 参数。

## iPhone 快捷指令怎么接？

先在快捷指令里放好两个值：服务器地址和 `API_TOKEN`。

然后照这个顺序做：

1. 接收分享文字或剪贴板文字。
2. POST 到 `/v1/jobs`，带上文字和 Token。
3. 用返回的 `poll_url` 查看进度，成功或失败后停止等待。
4. 成功后，逐个读取 `media`，按下面的类型保存。

| `kind` | 怎么保存 |
| --- | --- |
| `video` | 下载 `url` → 系统“存储到相簿” |
| `image` | 下载 `url` → 系统“存储到相簿” |
| `live_photo` | 把同一项的 `url` 和 `motion_url` 交给第三方 Live Photo 快捷指令库 |

`motion_photo_url` 是给支持 Motion Photo 的安卓相册使用的文件。

[中文逐步说明](docs/SHORTCUTS.md) · [English step-by-step guide](docs/SHORTCUTS.en.md)

## 用域名访问

让你的反代指向服务器的对外端口，例如 `8099`。使用默认配置时，后端会根据请求和反代头生成 `poll_url` 与下载地址。

反代要传递原来的域名和协议；如果使用 `/media` 这样的路径前缀，还要传递前缀。具体示例见 [部署和接口说明](docs/DOCKER_API.md)。`PUBLIC_BASE_URL` 通常可以留空。

## 看日志、更新和停止

```bash
# 看发生了什么
docker compose logs --tail=100 -f

# 获取新版本并更新
git pull --ff-only
docker compose up -d --build

# 停止
docker compose down
```

下载文件和任务记录放在 Docker 数据卷中。正常停止或更新不会清空数据；到期文件会自动清理。

## 给开发者

[核心原理和测试方法](docs/DEVELOPMENT.md) · [Docker API](docs/DOCKER_API.md)

X 先使用容器内的 Python 解析器；会话不可见、解析失败或下载失败时，最多切换一次免费 FxTwitter API。备用方式也失败时，任务会结束为 `failed`。

## 开源说明

本项目使用 [AGPL-3.0](LICENSE)。如果修改后通过网络提供服务，也需要按该协议提供对应源码。运行中的服务在 `/source.tar.gz` 提供当前版本的源码，不包含 `.env`、Cookie 或下载文件。

抖音、小红书、B 站和 Motion Photo 的部分逻辑改编自 [YunX-Desktop](https://github.com/tidain/YunX-Desktop)。X 解析逻辑参考 [FxEmbed](https://github.com/FxEmbed/FxEmbed)，相关 MIT 许可证保留在 [vendor/fxembed/LICENSE.md](vendor/fxembed/LICENSE.md)。详细来源见 [NOTICE](NOTICE)。

请下载你有权保存的内容。
