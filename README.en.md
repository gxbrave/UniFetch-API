# UniFetch-API

[中文](README.md) | **English**

Send sharing text. It gets the videos and pictures ready, then gives you download links.

**Installation needs just one Docker Compose file. The image includes every required program, and the service runs in one container.**

## How do I install it?

You only need **Docker Compose** installed.

1. Paste the file below into your Docker panel, or save it as `compose.yaml`.
2. Fill in `API_TOKEN` with your own random password of at least 24 ASCII characters. This is your “key”.
3. Click Deploy, or run `docker compose up -d` in the file's folder.

You can leave the other settings alone. Empty Cookies mean no account Cookie is used.

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

The image already includes Python, Chromium, FFmpeg, and all project dependencies. Both **amd64 and arm64** are supported; Docker chooses the right version for your machine.

Open `http://YOUR_SERVER_IP:8099` after startup. API instructions are at `/docs`; the health check is at `/health`.

## What can it download?

| Platform | Files |
| --- | --- |
| Douyin | Videos, pictures, and matching picture/video pairs for Live Photos |
| Xiaohongshu | Videos and pictures; some posts need a Cookie |
| Bilibili | Videos; quality depends on login and account access |
| X / Twitter | Videos, animated GIF videos, and original pictures; a free backup API is used if built-in parsing fails |

It picks the best quality available for that request. It cannot create a quality level the platform does not offer.

## How do I change settings?

Edit the values in your Compose file, then deploy it again.

| Setting | When empty | Purpose |
| --- | --- | --- |
| `API_TOKEN` | Required | Your API key; at least 24 characters |
| `CONTAINER_PORT` | `8000` | Inside port; the right side of `ports` must match if changed |
| The four `*_COOKIE` values | Not used | Your login Cookie for each platform |
| `PUBLIC_BASE_URL` | Detected automatically | Usually leave empty; set only for a fixed domain and path |
| `TRUST_PROXY_HEADERS` | `true` | Use the host and protocol passed by your reverse proxy |
| `RETENTION_HOURS` | `24` | Hours to keep downloaded files |
| `MAX_PENDING_JOBS` | `10` | Maximum number of queued or running jobs |

`8099:8000` means “server port 8099 connects to container port 8000”. To change only the outside port, edit the left number.

A Cookie is a platform's login pass. Try leaving it empty first. An X Cookie needs both `auth_token` and `ct0`. If a Cookie contains `$`, write it as `$$` in Compose; one literal `$` reaches the container. X Cookies are never sent to the free backup API.

Files and jobs live in a Docker volume. Normal stops and updates keep your data. Expired files are cleaned up automatically.

## How do I send a link?

Replace `YOUR_TOKEN` with your key and use your own server address:

```bash
curl -X POST 'http://localhost:8099/v1/jobs' \
  -H 'Authorization: Bearer YOUR_TOKEN' \
  -H 'Content-Type: application/json' \
  -d '{"text":"Paste sharing text containing a link here"}'
```

It returns a `poll_url`: “come here to check progress”. Visit it every two seconds using the same Token:

- `queued` / `running`: still getting ready; wait a little.
- `succeeded`: download the files in `media`, then stop waiting.
- `failed`: show `error.message`, then stop waiting.

Use complete download URLs, including `expires` and `sig`. File downloads do not need the API Token.

## iPhone Shortcuts

Follow this order: **read sharing text → submit a job → wait for the result → save the media**.

| `kind` | How to save |
| --- | --- |
| `video` / `image` | Download `url`, then use Apple's “Save to Photo Album” action |
| `live_photo` | Send the same item's `url` and `motion_url` to a third-party Live Photo Shortcut library |

Each picture is already matched to its motion video. Your chosen library creates and saves the iPhone Live Photo. `motion_photo_url` is for Android galleries that support Motion Photos.

[English Shortcut guide](docs/SHORTCUTS.en.md) · [中文说明](docs/SHORTCUTS.md)

## Updates, logs, and stopping

Run these commands in the folder containing `compose.yaml`:

```bash
# Update
docker compose pull
docker compose up -d

# Read logs
docker compose logs --tail=100 -f

# Stop
docker compose down
```

Console logs are enabled by default. They show incoming requests, queue positions, execution steps, download progress, and final results. Lines for the same job share its `job` ID. `queue_position=2` means one job is ahead, including a currently running job. Tokens, Cookies, and signed URL parameters are omitted.

## More details

[Reverse proxy and API](docs/DOCKER_API.md) · [Developers: source and tests](docs/DEVELOPMENT.md)

This project uses [AGPL-3.0](LICENSE). If you modify it and offer it over a network, provide the corresponding source under that license. The running service includes source and licenses at `/source.tar.gz`, without Tokens, Cookies, or downloads.

Some logic is adapted from [YunX-Desktop](https://github.com/tidain/YunX-Desktop); the X parser is based on [FxEmbed](https://github.com/FxEmbed/FxEmbed). See [NOTICE](NOTICE) for attribution. Download content you have permission to save.
