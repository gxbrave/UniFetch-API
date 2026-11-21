# UniFetch-API

[中文](README.md) | **English**

Give it a shared link. It gets the videos and pictures ready, then gives you download links.

Think of it as a little helper:

1. You send it some sharing text.
2. It finds the link and gets the files ready.
3. Your Shortcut picks up the files and saves them to Photos.

It runs on your server. **You only need one Docker container.**

## What can it get?

| Platform | Files you can download |
| --- | --- |
| Douyin | Videos, pictures, and the picture/video pair for a Live Photo |
| Xiaohongshu | Videos and pictures; some posts need a Cookie |
| Bilibili | Videos; available quality depends on login and account access |
| X / Twitter | Videos, animated GIF videos, and original pictures; a free backup API is used when built-in parsing fails |

It picks the **best quality available for that request**. It does not enlarge a blurry file and call it high quality. It cannot create a quality level the platform does not offer.

For Live Photos, it returns a matching picture link and video link. A third-party iPhone Shortcut library must turn that pair into a Live Photo. This backend prepares the files.

## Start in three steps

Use a computer or server with **Git and Docker Compose** installed.

### 1. Get the project

```bash
git clone https://github.com/gxbrave/UniFetch-API.git
cd UniFetch-API
cp .env.example .env
```

### 2. Fill in the little settings sheet

Open `.env` in a text editor.

**The only required change is `API_TOKEN`.** It is your door key. Replace the example with your own random password of at least 24 characters. Do not use the example text as your password, and do not share your key.

You can run `openssl rand -hex 32` to make a key, then paste its output into the file.

You can leave the ports and Cookies empty. Empty ports use the defaults. Empty Cookies mean no Cookie is used.

```dotenv
API_TOKEN=replace-with-your-own-random-password-at-least-24-characters

PORT=
CONTAINER_PORT=

DOUYIN_COOKIE=
XIAOHONGSHU_COOKIE=
BILIBILI_COOKIE=
X_COOKIE=
```

Values in `.env` become Docker environment variables. You can also set Cookies directly in the `environment` section of `compose.yaml`. Changing ports through `.env` updates the port mapping too.

### 3. Start your helper

```bash
docker compose up -d --build
docker compose ps
```

The first start downloads tools and builds the image. Give it a little time.

On the server, open:

- Home: `http://localhost:8099`
- API instructions: `http://localhost:8099/docs`
- Health check: `http://localhost:8099/health`

On another device, replace `localhost` with your server's IP address or domain.

## What do the settings mean?

| Environment variable | When empty | What it does |
| --- | --- | --- |
| `API_TOKEN` | Required | Your API key; at least 24 characters |
| `PORT` | `8099` | The outside port on your server |
| `CONTAINER_PORT` | `8000` | The port used inside the container |
| `HOST_BIND` | `0.0.0.0` | The server IP to listen on; all network interfaces by default |
| `DOUYIN_COOKIE` | Not used | Your Douyin login Cookie |
| `XIAOHONGSHU_COOKIE` | Not used | Your Xiaohongshu login Cookie |
| `BILIBILI_COOKIE` | Not used | Your Bilibili login Cookie |
| `X_COOKIE` | Not used | Your X login Cookie; include both `auth_token` and `ct0` |
| `PUBLIC_BASE_URL` | Detected automatically | Set only if you want a fixed public URL and path prefix |
| `TRUST_PROXY_HEADERS` | `true` | Use reverse proxy headers to build public URLs |
| `RETENTION_HOURS` | `24` | How many hours to keep downloaded files |
| `MAX_PENDING_JOBS` | `10` | Maximum number of queued or running jobs |

For example, `PORT=9000` and `CONTAINER_PORT=8100` connect server port `9000` to container port `8100`. You can change either setting on its own. The app and its health check both follow the container port.

A Cookie is a platform's login pass. Try public content without one first. If login is needed, copy that platform's Cookie from your own browser. Put single quotes around a Cookie in `.env` if it contains `$` or `#`. X Cookies are sent only to X's official API, never to the free backup API.

**After editing `.env`, run `docker compose up -d` again to apply the changes.**

## How do I ask it for a file?

### Send a link

Replace `YOUR_TOKEN` with your key and replace the sharing text with real content:

```bash
curl -X POST 'http://localhost:8099/v1/jobs' \
  -H 'Authorization: Bearer YOUR_TOKEN' \
  -H 'Content-Type: application/json' \
  -d '{"text":"Paste sharing text containing a link here"}'
```

It returns a `poll_url`. That means “come here to check progress.”

### Check if it is ready

Visit `poll_url` every two seconds with the same `Authorization` header.

| `status` | Meaning | What to do |
| --- | --- | --- |
| `queued` | Waiting in line | Wait a little |
| `running` | Getting files ready | Wait a little |
| `succeeded` | Ready | Download the items in `media` and stop waiting |
| `failed` | This attempt did not work | Show `error.message` and stop waiting |

Each download URL already has a temporary pass. File downloads do not need the API Token. Use the full URL, including its `expires` and `sig` parameters.

## How do I connect an iPhone Shortcut?

Store two values in your Shortcut: your server URL and your `API_TOKEN`.

Then follow these steps:

1. Read sharing text or text from the clipboard.
2. POST the text to `/v1/jobs` with your Token.
3. Check the returned `poll_url`. Stop waiting on success or failure.
4. On success, read each item in `media` and save it according to its type.

| `kind` | How to save it |
| --- | --- |
| `video` | Download `url` → Apple's “Save to Photo Album” action |
| `image` | Download `url` → Apple's “Save to Photo Album” action |
| `live_photo` | Pass that item's `url` and `motion_url` to a third-party Live Photo Shortcut library |

`motion_photo_url` is a file for Android galleries that support Motion Photos.

[English step-by-step guide](docs/SHORTCUTS.en.md) · [中文逐步说明](docs/SHORTCUTS.md)

## Use your own domain

Point your reverse proxy at your server's outside port, such as `8099`. With the default settings, the backend builds `poll_url` and download URLs from the request and proxy headers.

Your proxy must pass the original host and protocol. If you use a path prefix such as `/media`, pass that prefix too. See the examples in [deployment and API details](docs/DOCKER_API.md). You can usually leave `PUBLIC_BASE_URL` empty.

## Logs, updates, and stopping

```bash
# See what is happening
docker compose logs --tail=100 -f

# Get the latest version and update
git pull --ff-only
docker compose up -d --build

# Stop
docker compose down
```

Downloaded files and job records live in a Docker volume. A normal stop or update keeps them. Expired files are cleaned up automatically.

## For developers

[Parser details and tests](docs/DEVELOPMENT.md) · [Docker API](docs/DOCKER_API.md)

X first uses the Python parser inside the same container. If the session cannot see a post, parsing fails, or media cannot be downloaded, it switches to the free FxTwitter API at most once. If the backup also fails, the job ends as `failed`.

## Open source

This project uses [AGPL-3.0](LICENSE). If you modify it and offer it over a network, you must provide the corresponding source under that license. The running service serves its current source at `/source.tar.gz`, without `.env`, Cookies, or downloaded files.

Some Douyin, Xiaohongshu, Bilibili, and Motion Photo logic is adapted from [YunX-Desktop](https://github.com/tidain/YunX-Desktop). The X parser is based on [FxEmbed](https://github.com/FxEmbed/FxEmbed). Its MIT license is kept in [vendor/fxembed/LICENSE.md](vendor/fxembed/LICENSE.md). See [NOTICE](NOTICE) for attribution.

Please download content you have permission to save.
