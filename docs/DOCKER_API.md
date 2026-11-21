# UniFetch-API：部署和接口

[简单中文介绍](../README.md) · [English README](../README.en.md)

## Docker Compose

```bash
cp .env.example .env
# 编辑 .env，设置自己的 API_TOKEN，至少 24 个字符。
docker compose up -d --build
docker compose ps
```

所有端口和平台 Cookie 都通过 Docker 环境变量配置。默认对外端口 `PORT=8099`，默认容器端口 `CONTAINER_PORT=8000`；两项都支持留空使用默认值。Compose 会同时设置容器的监听端口、健康检查和端口映射。

例如：

```dotenv
API_TOKEN=replace-with-your-own-random-secret-at-least-24-characters
PORT=9000
CONTAINER_PORT=8100
DOUYIN_COOKIE=
XIAOHONGSHU_COOKIE=
BILIBILI_COOKIE=
X_COOKIE=
```

此时从服务器的 `9000` 端口访问，容器内部监听 `8100`。Cookie 留空时不携带账号登录凭证。平台 Cookie 包含 `$` 或 `#` 时，在 `.env` 中用单引号包住整段值。修改配置后执行 `docker compose up -d`，不要只执行 `restart`。

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
docker compose up -d --build
```

任务和媒体保存在 `media-data` 命名卷中。正常停止或更新不会删除该卷。重启时仍未完成的任务会标记为失败，请重新提交。

- `/health`：健康检查。
- `/docs`：可交互的 OpenAPI 文档。
- `/shortcuts`：中文快捷指令配置说明。
- `/source.tar.gz`：当前运行版本的 AGPL-3.0 对应源码，含中英文 README、构建配置、测试和第三方许可证，不含 `.env`、Cookie 或下载文件。
