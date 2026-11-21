from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict

from .core import ParseError, parse, save_result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="独立的抖音 / 小红书 / 哔哩哔哩 / X 解析下载器")
    parser.add_argument("url", help="作品链接或完整分享文案")
    parser.add_argument("-o", "--output", default="downloads", help="下载目录，默认 downloads")
    parser.add_argument("--cookie", default="", help="平台 Cookie，格式为浏览器复制的 Cookie 头；X 为可选")
    parser.add_argument("--browser", default=None, help="Chrome/Chromium 可执行文件路径，抖音解析用")
    parser.add_argument("--quality", type=int, default=None, help="B 站清晰度编号，例如 80、112、120")
    parser.add_argument("--parse-only", action="store_true", help="只解析直链，不下载媒体")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出解析结果")
    return parser


def _serialise(result):
    value = asdict(result)
    value.pop("_x_public_used", None)
    value["platform"] = result.platform.value
    value["medias"] = [dict(item) for item in value["medias"]]
    for item in value["medias"]:
        item["kind"] = item["kind"].value if hasattr(item["kind"], "value") else item["kind"]
    return value


def main() -> int:
    args = _parser().parse_args()
    try:
        result = parse(args.url, cookie=args.cookie, browser=args.browser, quality=args.quality)
        if args.json:
            print(json.dumps(_serialise(result), ensure_ascii=False, indent=2))
        else:
            print(f"平台: {result.platform.value}")
            print(f"标题: {result.title}")
            print(f"作者: {result.author or '未知'}")
            print(f"媒体: {len(result.medias)} 个，视频 {result.video_count} 个，图片 {result.image_count} 张")
            for index, media in enumerate(result.medias, start=1):
                print(f"  [{index}] {media.kind.value} {media.quality or ''} {media.url}")
            if result.notice:
                print(f"提示: {result.notice}")
        if not args.parse_only:
            paths = save_result(result, args.output, cookie=args.cookie)
            print("已保存:")
            for path in paths:
                print(f"  {path}")
    except ParseError as exc:
        print(f"解析失败（{exc.category}）: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("已取消", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
