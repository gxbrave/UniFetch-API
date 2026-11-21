import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests

from yunx_downloader.core import (
    MediaKind, ParseError, _parse_x, _parse_x_public, _parse_x_graphql_payload,
    _x_graphql_payload, detect_link, save_result,
)


LINK = detect_link("https://x.com/test/status/2115039599554941098")
TWEET = {
    "__typename": "Tweet", "rest_id": LINK.id,
    "core": {"user_results": {"result": {"core": {"name": "tester", "screen_name": "test"}}}},
    "legacy": {
        "full_text": "truncated text https://t.co/media",
        "extended_entities": {"media": [
            {"type": "photo", "url": "https://t.co/media",
             "media_url_https": "https://pbs.twimg.com/media/a.jpg:large?name=small&format=jpg"},
            {"type": "video", "media_url_https": "https://pbs.twimg.com/thumb.jpg",
             "original_info": {"width": 720, "height": 1280},
             "video_info": {"variants": [
                 {"url": "https://video.twimg.com/vid/avc1/720x1280/low.mp4", "bitrate": 1000, "content_type": "video/mp4"},
                 {"url": "https://video.twimg.com/vid/avc1/1080x1920/high.mp4", "bitrate": 9000, "content_type": "video/mp4"},
                 {"url": "https://video.twimg.com/playlist.m3u8", "bitrate": 20000, "content_type": "application/x-mpegURL"},
             ]}},
        ]},
    },
    "note_tweet": {"note_tweet_results": {"result": {"text": "完整长文 &amp; 图片 https://t.co/media"}}},
}


def envelope(tweet=None):
    return {"data": {"tweetResult": {"result": copy.deepcopy(TWEET if tweet is None else tweet)}}}


def response(payload, status=200):
    result = requests.Response()
    result.status_code = status
    result._content = json.dumps(payload).encode()
    return result


class XTest(unittest.TestCase):
    def test_raw_graphql_preserves_media_order_original_photo_and_best_video(self):
        parsed = _parse_x_graphql_payload(envelope(), LINK)
        self.assertEqual(parsed.author, "tester")
        self.assertEqual(parsed.canonical_url, LINK.url)
        self.assertEqual(parsed.text, "完整长文 & 图片")
        self.assertEqual([media.kind for media in parsed.medias], [MediaKind.IMAGE, MediaKind.VIDEO])
        self.assertEqual(parsed.medias[0].url, "https://pbs.twimg.com/media/a.jpg?name=orig&format=jpg")
        self.assertTrue(parsed.medias[1].url.endswith("high.mp4"))
        self.assertIn("1080P", parsed.medias[1].quality)
        self.assertIn("9 kbps", parsed.medias[1].quality)

    def test_visibility_wrapper_and_repost_are_unwrapped(self):
        for node in (
            {"__typename": "TweetWithVisibilityResults", "tweet": TWEET},
            {"__typename": "Tweet", "legacy": {"retweeted_status_results": {"result": TWEET}}},
        ):
            with self.subTest(node=node["__typename"]):
                self.assertEqual(len(_parse_x_graphql_payload(envelope(node), LINK).medias), 2)

    def test_gif_mp4_without_bitrate_is_downloadable(self):
        tweet = copy.deepcopy(TWEET)
        video = tweet["legacy"]["extended_entities"]["media"][1]
        video["type"] = "animated_gif"
        video["video_info"]["variants"] = [{"url": "https://video.twimg.com/gif.mp4", "content_type": "video/mp4"}]
        self.assertEqual(_parse_x_graphql_payload(envelope(tweet), LINK).medias[1].url, "https://video.twimg.com/gif.mp4")

    def test_hls_only_never_downloads_thumbnail_as_video(self):
        tweet = copy.deepcopy(TWEET)
        video = tweet["legacy"]["extended_entities"]["media"][1]
        video["video_info"]["variants"] = [{"url": "https://video.twimg.com/playlist.m3u8", "content_type": "application/x-mpegURL"}]
        with self.assertRaises(ParseError) as error:
            _parse_x_graphql_payload(envelope(tweet), LINK)
        self.assertEqual(error.exception.category, "unsupported")

    def test_guest_flow_uses_post_then_x_graphql_with_long_text_features(self):
        session = Mock(spec=requests.Session)
        session.post.return_value = response({"guest_token": "guest-test"})
        session.get.return_value = response(envelope())
        parsed = _parse_x_graphql_payload(_x_graphql_payload(session, LINK.id), LINK)
        session.post.assert_called_once()
        self.assertEqual(session.post.call_args.args[0], "https://api.x.com/1.1/guest/activate.json")
        self.assertTrue(session.get.call_args.args[0].startswith("https://api.x.com/graphql/"))
        kwargs = session.get.call_args.kwargs
        self.assertEqual(kwargs["headers"]["x-guest-token"], "guest-test")
        self.assertTrue(json.loads(kwargs["params"]["features"])["longform_notetweets_consumption_enabled"])
        self.assertEqual(json.loads(kwargs["params"]["variables"])["tweetId"], LINK.id)
        self.assertFalse(kwargs["allow_redirects"])
        self.assertNotIn("Cookie", parsed.medias[0].headers)

    def test_auth_session_does_not_depend_on_guest_token_activation(self):
        session = Mock(spec=requests.Session)
        session.get.return_value = response(envelope())
        _x_graphql_payload(session, LINK.id, cookie="auth_token=test-only; ct0=csrf-test")
        session.post.assert_not_called()
        headers = session.get.call_args.kwargs["headers"]
        self.assertEqual(headers["x-csrf-token"], "csrf-test")
        self.assertEqual(headers["x-twitter-auth-type"], "OAuth2Session")
        self.assertNotIn("x-guest-token", headers)

    def test_invalid_auth_missing_csrf_fails_before_request(self):
        session = Mock(spec=requests.Session)
        with self.assertRaises(ParseError):
            _x_graphql_payload(session, LINK.id, cookie="auth_token=test-only")
        session.get.assert_not_called()
        session.post.assert_not_called()

    def test_tombstone_and_rate_limit_have_distinct_errors(self):
        with self.assertRaises(ParseError) as error:
            _parse_x_graphql_payload(envelope({"__typename": "TweetTombstone"}), LINK)
        self.assertEqual(error.exception.category, "access_restricted")
        session = Mock(spec=requests.Session)
        session.post.return_value = response({"errors": [{"code": 88}]})
        with self.assertRaises(ParseError) as error:
            _x_graphql_payload(session, LINK.id)
        self.assertEqual(error.exception.category, "blocked")
        session.get.assert_not_called()

    def test_builtin_success_does_not_call_public_api(self):
        with patch("yunx_downloader.core._x_graphql_payload", return_value=envelope()), \
                patch("yunx_downloader.core._parse_x_public") as fallback:
            parsed = _parse_x(Mock(), LINK)
        self.assertFalse(parsed._x_public_used)
        fallback.assert_not_called()

    def test_restricted_or_failed_builtin_request_calls_public_api_once(self):
        session = Mock()
        for cause in (ParseError("unavailable", "access_restricted"), ParseError("network", "network")):
            with self.subTest(category=cause.category), \
                    patch("yunx_downloader.core._x_graphql_payload", side_effect=cause), \
                    patch("yunx_downloader.core._parse_x_public", side_effect=ParseError("unavailable", "not_found")) as fallback:
                with self.assertRaises(ParseError):
                    _parse_x(session, LINK, cookie="auth_token=test-only; ct0=csrf-test")
                fallback.assert_called_once_with(session, LINK)

    def test_public_api_uses_best_mp4_and_receives_no_account_cookie(self):
        session = Mock(spec=requests.Session)
        session.get.return_value = response({"code": 200, "tweet": {
            "id": LINK.id, "text": "public post", "author": {"name": "tester", "screen_name": "test"},
            "media": {"all": [{"type": "video", "url": "https://video.twimg.com/low.mp4",
                "width": 720, "height": 1280,
                "formats": [
                    {"url": "https://video.twimg.com/vid/avc1/1080x1920/high.mp4", "container": "mp4", "bitrate": 10368000},
                    {"url": "https://video.twimg.com/high.m3u8", "container": "m3u8", "bitrate": 20000000},
                ]}]},
        }})
        parsed = _parse_x_public(session, LINK)
        self.assertTrue(parsed._x_public_used)
        self.assertTrue(parsed.medias[0].url.endswith("high.mp4"))
        self.assertIn("10368 kbps", parsed.medias[0].quality)
        headers = session.get.call_args.kwargs["headers"]
        self.assertNotIn("Cookie", headers)
        self.assertNotIn("Authorization", headers)
        self.assertEqual(session.get.call_args.args[0], "https://api.fxtwitter.com/status/" + LINK.id)

    def test_cli_download_failure_retries_once_and_public_failure_is_terminal(self):
        original = _parse_x_graphql_payload(envelope(), LINK)
        fallback_result = copy.deepcopy(original)
        fallback_result._x_public_used = True
        for retry_succeeds in (True, False):
            with self.subTest(retry_succeeds=retry_succeeds), tempfile.TemporaryDirectory() as folder:
                outcome = [ParseError("download failed", "network"), [Path(folder) / "saved.mp4"] if retry_succeeds else ParseError("again", "network")]
                with patch("yunx_downloader.core._save_result", side_effect=outcome) as download, \
                        patch("yunx_downloader.core._x_public_retry", return_value=fallback_result) as fallback:
                    if retry_succeeds:
                        self.assertEqual(save_result(original, folder), [Path(folder) / "saved.mp4"])
                    else:
                        with self.assertRaises(ParseError):
                            save_result(original, folder)
                    fallback.assert_called_once_with(original)
                    self.assertEqual(download.call_count, 2)
        with patch("yunx_downloader.core._save_result", side_effect=ParseError("download failed")), \
                patch("yunx_downloader.core._x_public_retry") as fallback:
            with self.assertRaises(ParseError):
                save_result(fallback_result, "unused")
            fallback.assert_not_called()
