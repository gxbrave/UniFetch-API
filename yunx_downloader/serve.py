"""Start the API and check its health using the same configurable port."""

import os
from urllib.request import urlopen


def container_port() -> int:
    value = os.environ.get("CONTAINER_PORT", "").strip() or "8000"
    try:
        port = int(value)
    except ValueError as error:
        raise ValueError("CONTAINER_PORT must be a number between 1 and 65535") from error
    if not 1 <= port <= 65535:
        raise ValueError("CONTAINER_PORT must be between 1 and 65535")
    return port


def check_health() -> None:
    with urlopen(f"http://127.0.0.1:{container_port()}/health", timeout=3) as response:
        if response.status != 200:
            raise RuntimeError("API is not healthy")


def main() -> None:
    import uvicorn

    uvicorn.run(
        "yunx_downloader.server:create_app", factory=True,
        host="0.0.0.0", port=container_port(), workers=1,
        proxy_headers=False, access_log=False,
    )


if __name__ == "__main__":
    main()
