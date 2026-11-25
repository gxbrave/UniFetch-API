import unittest
import json
from unittest.mock import patch
from urllib.parse import quote

import requests

from yunx_downloader.core import (Media, MediaKind, _douyin_best_video, _douyin_image_urls,
                                 _douyin_page_detail, _parse_douyin, detect_link)


class CoreTest(unittest.TestCase):
    def test_flight_data_preserves_static_and_live_image_positions(self):
        detail = {"awemeId": "12345678901", "images": [
            {"urlList": ["https://example/a.jpg"], "video": {"playAddr": [{"src": "https://example/a.mp4"}]}},
            {"urlList": ["https://example/b.jpg"], "video": None},
            {"urlList": ["https://example/c.jpg"], "video": {"playAddr": [{"src": "https://example/c.mp4"}]}},
        ]}
        record = "7:" + json.dumps(["$", "element", {"detail": detail}]) + "\n"
        split = len(record) // 2
        body = "".join("<script>self.__pace_f.push(" + json.dumps([1, chunk]) + ")</script>" for chunk in (record[:split], record[split:]))
        self.assertEqual(_douyin_page_detail(body, "12345678901"), detail)
        self.assertIsNone(_douyin_page_detail(body, "99999999999"))

    def test_byte_counted_flight_text_does_not_hide_following_live_pairs(self):
        # Real /note pages put a text row immediately before the work row,
        # without a newline. Its hex length counts UTF-8 bytes, not characters.
        detail = {"awemeId": "7687913416032433338", "desc": "中国人会飞", "images": [
            {"urlList": [f"https://example/{i}.jpg"],
             "video": {"playAddr": [{"src": f"https://example/{i}.mp4"}]}}
            for i in range(7)
        ]}
        text = json.dumps({"title": "中文🎈\n7:这不是记录", "image": "普通文本"}, ensure_ascii=False)
        row = "a:T" + format(len(text.encode("utf-8")), "x") + "," + text
        row += '7:' + json.dumps(["$", "$L9", None, {"aweme": {"detail": detail}}], ensure_ascii=False) + "\n"
        # Script chunk boundaries need not coincide with Flight row boundaries.
        chunks = [row[:5], row[5:17], row[17:-35], row[-35:]]
        body = "".join("<script>self.__pace_f.push(" + json.dumps([1, chunk]) + ")</script>" for chunk in chunks)
        self.assertEqual(_douyin_page_detail(body, detail["awemeId"]), detail)
        self.assertIsNone(_douyin_page_detail(body, "99999999999"))
        with patch("yunx_downloader.core._browser_dump", return_value=body):
            result = _parse_douyin(requests.Session(), detect_link("https://www.douyin.com/note/" + detail["awemeId"]),
                                   cookie="", timeout=90, browser=None)
        self.assertEqual([(m.url, m.motion_url) for m in result.medias],
                         [(f"https://example/{i}.jpg", f"https://example/{i}.mp4") for i in range(7)])
        self.assertEqual(result.notice, "")

    def test_flight_text_json_can_contain_raw_or_url_encoded_mixed_album(self):
        detail = {"awemeId": "12345678901", "images": [
            {"urlList": ["https://example/a.jpg?sign=%22value%22"], "video": None},
            {"urlList": ["https://example/b.jpg"], "video": {"playAddr": [{"src": "https://example/b.mp4"}]}},
        ]}
        raw = json.dumps({"detail": detail})
        for text in (raw, quote(raw)):
            with self.subTest(encoded=text.startswith("%")):
                row = "8:T" + format(len(text.encode("utf-8")), "x") + "," + text
                body = "<script>self.__pace_f.push(" + json.dumps([1, row]) + ")</script>"
                self.assertEqual(_douyin_page_detail(body, detail["awemeId"]), detail)
                with patch("yunx_downloader.core._browser_dump", return_value=body):
                    result = _parse_douyin(requests.Session(), detect_link("https://www.douyin.com/note/" + detail["awemeId"]),
                                           cookie="", timeout=90, browser=None)
                self.assertEqual([m.url for m in result.medias], [image["urlList"][0] for image in detail["images"]])
                self.assertEqual([m.motion_url for m in result.medias], ["", "https://example/b.mp4"])

    def test_truncated_flight_text_does_not_parse_its_contents_as_a_work(self):
        detail = {"awemeId": "12345678901", "images": [{"urlList": ["https://example/a.jpg"]}]}
        row = '8:Tffff,7:' + json.dumps(detail) + "\n"
        body = "<script>self.__pace_f.push(" + json.dumps([1, row]) + ")</script>"
        self.assertIsNone(_douyin_page_detail(body, detail["awemeId"]))

    def test_share_text_rules(self):
        self.assertEqual(detect_link("复制 https://v.douyin.com/U_XuuBSHU6c/").platform.value, "douyin")
        self.assertEqual(detect_link("https://www.bilibili.com/video/BV1GJ411x7h7").id, "BV1GJ411x7h7")
        self.assertEqual(
            detect_link("https://www.xiaohongshu.com/explore/65f0a1b2c3d4e5f60718293a").platform.value,
            "xiaohongshu",
        )
        self.assertEqual(detect_link("https://x.com/kittyovo133/status/2115039599554941098").platform.value, "x")
        self.assertEqual(detect_link("https://twitter.com/kittyovo133/status/2115039599554941098").id, "2115039599554941098")

    def test_douyin_image_variants_are_collapsed(self):
        body = "".join(
            f'<img src="https://p3-pc-sign.douyinpic.com/tos/bucket/token~tplv-dy-aweme-images:{size}.webp?biz_tag=aweme_images">'
            for size in ("3000:3000", "1440:1920", "1920:1920")
        )
        self.assertEqual(len(_douyin_image_urls(body)), 1)

    def test_media_model_keeps_motion_pair(self):
        media = Media("https://example/image.webp", MediaKind.IMAGE, motion_url="https://example/live.mp4")
        self.assertEqual(media.kind, MediaKind.IMAGE)
        self.assertTrue(media.motion_url)

    def test_douyin_best_video_uses_nested_play_addr_size(self):
        detail = {
            "video": {
                "width": 576,
                "height": 1024,
                "play_addr": {"url_list": ["https://example/default.mp4"]},
                "bit_rate": [
                    {
                        "gear_name": "720p",
                        "bit_rate": 1000,
                        "play_addr": {
                            "data_size": 100,
                            "width": 720,
                            "height": 1280,
                            "url_list": ["https://example/720.mp4"],
                        },
                    },
                    {
                        "gear_name": "4k",
                        "bit_rate": 2000,
                        "play_addr": {
                            "data_size": 500,
                            "width": 2160,
                            "height": 3840,
                            "url_list": ["https://example/4k.mp4"],
                        },
                    },
                ],
            }
        }
        url, quality = _douyin_best_video(detail)
        self.assertEqual(url, "https://example/4k.mp4")
        self.assertEqual(quality, "4K")


if __name__ == "__main__":
    unittest.main()
