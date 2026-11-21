"""Download API job media with stable filenames and explicit Live Photo pairs."""

import json
import shutil
import subprocess
from dataclasses import replace
from pathlib import Path

import requests
from PIL import Image

from .core import MediaKind, ParseError, ParseResult, _download_file, _write_motion_photo, save_result


def video_info(path: Path) -> dict:
    completed = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
         "stream=codec_name,width,height:format=duration", "-of", "json", str(path)],
        check=True, capture_output=True, timeout=30,
    )
    data = json.loads(completed.stdout)
    stream = (data.get("streams") or [{}])[0]
    if not stream.get("width"):
        raise ParseError("下载结果缺少有效的视频轨", "unknown")
    return {
        "width": stream["width"], "height": stream["height"],
        "codec": stream.get("codec_name", ""),
        "duration": float((data.get("format") or {}).get("duration") or 0),
    }


def _iphone_container(path: Path) -> dict:
    info = video_info(path)
    temporary = path.with_name(path.stem + ".remux.mp4")
    args = ["ffmpeg", "-y", "-v", "error", "-i", str(path), "-map", "0", "-c", "copy"]
    if info["codec"] == "hevc":
        args += ["-tag:v", "hvc1"]
    subprocess.run(args + ["-movflags", "+faststart", str(temporary)], check=True, capture_output=True, timeout=120)
    temporary.replace(path)
    return info


def prepare_media(result: ParseResult, directory: Path) -> list[dict]:
    directory.mkdir(parents=True, exist_ok=True)
    session = requests.Session()
    items = []
    try:
        for index, media in enumerate(result.medias, 1):
            stem = f"{index:03d}"
            if media.kind is MediaKind.VIDEO:
                # Retry the whole X job at the orchestration layer so refreshed
                # media order, filenames and reported quality stay consistent.
                paths = save_result(replace(result, title=stem, medias=[media]), directory, x_fallback=False)
                path = paths[-1]
                info = _iphone_container(path)
                items.append({"kind": "video", "quality": media.quality, "file": path.name,
                              "bytes": path.stat().st_size, **info})
                continue
            if media.kind is not MediaKind.IMAGE:
                raise ParseError("API 当前支持视频和图片", "unsupported")
            original = directory / f"{stem}.original"
            _download_file(session, media.url, original, media.headers, timeout=120)
            image_path = directory / f"{stem}.jpg"
            with Image.open(original) as image:
                width, height = image.size
                if image.format == "JPEG":
                    shutil.copyfile(original, image_path)
                else:
                    image.convert("RGB").save(image_path, "JPEG", quality=98, subsampling=0)
            item = {"kind": "image", "quality": media.quality, "file": image_path.name,
                    "bytes": image_path.stat().st_size, "width": width, "height": height}
            if media.motion_url:
                motion = directory / f"{stem}.motion.mp4"
                _download_file(session, media.motion_url, motion, media.headers, timeout=120)
                info = _iphone_container(motion)
                android = directory / f"{stem}.motion.jpg"
                if not _write_motion_photo(image_path, motion, android):
                    raise ParseError("动态照片封装失败", "unknown")
                item.update({"kind": "live_photo", "motion_file": motion.name,
                             "motion_photo_file": android.name, "duration": info["duration"],
                             "motion_width": info["width"], "motion_height": info["height"]})
            items.append(item)
    finally:
        session.close()
    return items
