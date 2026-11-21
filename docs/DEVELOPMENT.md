# Parser and development notes

UniFetch-API runs one Python API process. It bundles Chromium for Douyin page challenges and FFmpeg for video inspection and remuxing. X parsing does not need a separate service or Node runtime.

The import package remains `yunx_downloader` for compatibility with the original standalone implementation. The project and installed distribution are named `unifetch-api`; the command line tool is `unifetch`.

## Run tests

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[api,test]'
python -m unittest discover -s tests -v
```

These tests use local fixtures and mocks. They do not need platform Cookies or live upstream services.

## Run without Docker

Install Chromium and FFmpeg yourself, then:

```bash
export API_TOKEN='replace-with-a-random-secret-at-least-24-characters'
export DATA_DIR='./data'
export CONTAINER_PORT='8000'
python -m yunx_downloader.serve
```

`CONTAINER_PORT` applies to the Python startup module as well as Docker. Unset or empty means `8000`. An invalid port is rejected when the process starts. Cookie settings use the same environment variables documented in the README.

## Command line

```bash
unifetch 'https://www.bilibili.com/video/BV1GJ411x7h7' --parse-only --json
unifetch 'SHARE_TEXT_OR_URL' -o downloads
```

`--cookie` accepts a platform Cookie. The API reads the platform-specific Cookie from environment variables instead. The legacy `yunx-downloader` command is retained as an alias.

## Data flow

`core.py` detects the platform and returns `ParseResult` and ordered `Media` items. `api_media.py` downloads the files, inspects videos, and prepares image/motion pairs. `server.py` stores job state in SQLite and issues expiring signed file URLs.

| Platform | How it finds files |
| --- | --- |
| Douyin | Resolves sharing links, runs Chromium when needed, reads page/detail data, and selects the best available video stream. Images and motion clips stay paired by image position. |
| Xiaohongshu | Preserves the sharing token, reads the note's page state, selects a video stream, and requests original images with a page-image fallback. |
| Bilibili | Reads video metadata and play URLs. With suitable login access, downloads DASH video/audio tracks and merges them without re-encoding. |
| X | Activates an anonymous guest session or uses optional server-side auth Cookies, then requests `TweetResultByRestId`. Unwraps visibility/repost containers and selects original photos or the highest-bitrate MP4. |

If built-in X parsing fails, or downloading/validating its media fails, the job switches at most once to the free `https://api.fxtwitter.com/status/{id}` endpoint. The public request does not receive the X account Cookie. Failed public requests or media downloads end the job as `failed`.

Live images expose an ordered still/motion pair and an Android Motion Photo JPEG. iPhone Live Photo creation belongs to the chosen third-party Shortcut library.

## Source and attribution

- [YunX-Desktop](https://github.com/tidain/YunX-Desktop), reference commit `00a030f778a65840e9be65baecb15497a94b04ec`, AGPL-3.0. Adapted modules are listed in [NOTICE](../NOTICE).
- [FxEmbed](https://github.com/FxEmbed/FxEmbed), reference commit `2e9097877863050749f7ab555ce0c77a8caa70b4`, MIT. Relevant unchanged reference files and the license are in [vendor/fxembed](../vendor/fxembed/UPSTREAM.md).

Platform page structures, GraphQL query IDs and access rules can change. Cookie support is optional and does not guarantee access to a given post or quality tier.
