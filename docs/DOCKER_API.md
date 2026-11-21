# UniFetch-API：部署和接口

[简单中文介绍](../README.md) · [English README](../README.en.md)

## Docker Compose

安装只需要一份 `compose.yaml`，内容见 [README](../README.md)。镜像 `ghcr.io/gxbrave/unifetch-api:latest` 已包含 Python、Chromium、FFmpeg 和全部依赖。填好 Compose 中的 `API_TOKEN` 后启动：

```bash
docker compose up -d
```

不需要 Git、源码、`.env` 文件或本地构建。整个服务只有一个容器。

端口和 Cookie 直接填写在 Compose 中。`CONTAINER_PORT` 留空时为 `8000`，需与 `ports` 右边的端口一致；默认对外映射为 `8099:8000`。例如改为容器内 `8100`、对外 `9000`，就同时填写 `CONTAINER_PORT: "8100"` 和 `ports: ["9000:8100"]`。

四个平台 Cookie 留空时不使用账号 Cookie。Cookie 中的 `$` 在 Compose 中需写成 `$$`，`#` 放在带引号的值中。修改配置后执行 `docker compose up -d`，不要只执行 `restart`。

抖音通常可以匿名请求公开作品。小红书和 B 站的内容、画质受登录与账号权限影响。可选的 `X_COOKIE` 需包含 `auth_token` 和 `ct0`；X 优先用容器内解析，失败时最多切换一次免费的公开 FxTwitter API。平台 Cookie 不会发给该备用 API。

## 域名和反代

`PUBLIC_BASE_URL` 留空时，后端根据每次请求生成地址。支持 `Forwarded`、`X-Forwarded-Host`、`X-Forwarded-Proto`、`X-Forwarded-Port` 和 `X-Forwarded-Prefix`。

例如，在 Nginx 站点中添加：

```nginx
location / {
    proxy_pass http://127.0.0.1:8099;
    proxy_set_header Host $http_host;
    proxy_set_header X-Forwarded-Host $http_host;
    proxy_set_header X-Forwarded-Proto $scheme;
}
```

如果部署在 `/media/` 路径下：

```nginx
location /media/ {
    proxy_pass http://127.0.0.1:8099/;
    proxy_set_header Host $http_host;
    proxy_set_header X-Forwarded-Host $http_host;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header X-Forwarded-Prefix /media;
}
```

示例适用于反代和后端在同一台服务器且后端监听默认地址的情况。跨服务器时，把 `127.0.0.1` 换成后端可达的地址。使用多层反代时，传递实际客户端入口的域名和协议。

如果需要固定入口，可填写 `PUBLIC_BASE_URL=https://media.example.com/media`。不信任转发头时可设 `TRUST_PROXY_HEADERS=false`。

## 提交任务

```http
POST /v1/jobs
Authorization: Bearer YOUR_TOKEN
Content-Type: application/json

{"text":"完整分享文字或作品链接"}
```

返回 HTTP `202` 和任务信息：

```json
{
  "id": "0123456789abcdef0123456789abcdef",
  "status": "queued",
  "poll_url": "https://media.example.com/v1/jobs/0123456789abcdef0123456789abcdef"
}
```

也支持 `X-API-Token: YOUR_TOKEN` 请求头。提交时还可以在 JSON 中携带 `token`，但后续查询仍需 Token 请求头。

## 查询任务

用同样的鉴权请求头访问返回的 `poll_url`。状态可能为 `queued`、`running`、`succeeded` 或 `failed`。成功或失败后都应停止轮询。

成功结果中的 `media` 是有序数组：

```json
{
  "status": "succeeded",
  "media": [
    {"kind":"video", "quality":"1080P", "width":1080, "height":1920, "url":"签名视频地址"},
    {"kind":"image", "url":"签名图片地址"},
    {
      "kind":"live_photo",
      "url":"签名静态图片地址",
      "motion_url":"签名动态视频地址",
      "motion_photo_url":"签名安卓 Motion Photo JPEG 地址"
    }
  ]
}
```

`live_photo` 的 `url` 和 `motion_url` 来自同一张动态图片。把这两个地址交给第三方 Live Photo 快捷指令库进行配对和保存。`motion_photo_url` 供兼容 Motion Photo 的安卓相册使用。

失败结果包含 `error.code` 和 `error.message`。X 的备用链路也失败后会正常返回 `failed`，不会一直重试。

## 下载文件

签名下载地址不需要 API Token，支持 HTTP Range。请原样保留 URL 中的 `expires` 和 `sig` 参数。Token 不会写入下载 URL。

默认保留 24 小时。过期后重新提交分享链接以获取新的文件地址。视频保留来源画质；HEVC 会无损重封装为带 `hvc1` 标记的 MP4，便于 iPhone 识别。

## 运维和源码

```bash
docker compose logs --tail=100 -f
docker compose down
docker compose up -d
```

控制台日志默认开启，用 `docker compose logs -f` 即可查看：

- `收到请求` / `请求完成`：请求 ID、方法、接口、HTTP 状态、耗时。
- `任务入队` / `排队位置更新`：任务 ID、当前位置、前方任务数、运行和等待数量。位置从 1 开始，包含正在执行的任务。
- `执行步骤`：平台解析、浏览器等待、备用 API、逐项下载、视频检查和 Live 图配对。
- `下载进度` / `下载完成`：每隔约 5 秒有数据时输出已下载字节和可用的百分比。
- `任务完成` / `任务失败`：数量、画质、存储大小、总耗时，或失败步骤和错误类别。

日志用 `request` 和 `job` 串联请求与后台任务。不输出分享原文、Token、Cookie、完整平台链接或下载签名；健康检查不持续刷屏。

任务和媒体保存在 `media-data` 命名卷中。正常停止或更新不会删除该卷。重启时仍未完成的任务会标记为失败，请重新提交。

- `/health`：健康检查。
- `/docs`：可交互的 OpenAPI 文档。
- `/shortcuts`：中文快捷指令配置说明。
- `/source.tar.gz`：当前运行版本的 AGPL-3.0 对应源码，含中英文 README、构建配置、测试和第三方许可证，不含 `.env`、Cookie 或下载文件。
