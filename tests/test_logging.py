import itertools
import re
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests
from fastapi.testclient import TestClient

from yunx_downloader.core import Media, MediaKind, ParseError, ParseResult, Platform, _download_file
from yunx_downloader.observability import bind_context, step
from yunx_downloader.server import Settings, create_app


class LoggingTest(unittest.TestCase):
    def test_request_queue_and_worker_logs_share_context_without_secrets(self):
        started = threading.Event()
        release = threading.Event()
        calls = []
        token = 'private-api-token-at-least-24-characters'
        private_cookie = 'private-cookie-value'
        private_query = 'private-link-signature'
        private_text = 'private-sharing-text'
        url = 'https://x.com/test/status/2115039599554941098'

        def parse(text, **kwargs):
            calls.append(text)
            if len(calls) == 1:
                started.set()
                if not release.wait(5):
                    raise ParseError('test timed out')
            return ParseResult(Platform.X, private_text, url,
                               medias=[Media('https://pbs.twimg.com/image.jpg?sig='+private_query, MediaKind.IMAGE,
                                             headers={'Cookie': private_cookie}, quality='原图')])

        def prepare(parsed, directory):
            step('下载测试图片', media_index=1, media_total=1)
            directory.mkdir(parents=True)
            (directory/'001.jpg').write_bytes(b'image')
            return [{'kind': 'image', 'file': '001.jpg', 'bytes': 5, 'quality': '原图'}]

        with tempfile.TemporaryDirectory() as folder:
            settings = Settings(token, Path(folder), max_pending=2)
            headers = {'Authorization': 'Bearer '+token}
            with self.assertLogs('yunx.api', level='INFO') as logs, \
                    patch('yunx_downloader.server.parse', side_effect=parse), \
                    patch('yunx_downloader.server.prepare_media', side_effect=prepare), \
                    TestClient(create_app(settings)) as client:
                try:
                    first = client.post('/v1/jobs?api_key='+private_query,
                                        json={'text': private_text+' '+url+'?token='+private_query, 'token': token}).json()
                    self.assertTrue(started.wait(2))
                    second = client.post('/v1/jobs', json={'text': url}, headers=headers).json()
                    full = client.post('/v1/jobs', json={'text': url}, headers=headers)
                    self.assertEqual(full.status_code, 429)
                    client.get('/health')
                    client.get('/v1/files/'+token+'/'+private_cookie+'?sig='+private_query)
                finally:
                    release.set()
                for job in (first, second):
                    for _ in range(100):
                        result = client.get(job['poll_url'], headers=headers).json()
                        if result['status'] in ('succeeded', 'failed'):
                            break
                        time.sleep(0.01)
                    self.assertEqual(result['status'], 'succeeded')
                file_url = result['media'][0]['url']
                self.assertEqual(client.get(file_url).content, b'image')
                self.assertEqual(client.post('/v1/jobs', json={'text': url}).status_code, 401)
            messages = '\n'.join(logs.output)
            for secret in (token, private_cookie, private_query, private_text, file_url.split('sig=')[1]):
                self.assertNotIn(secret, messages)
            self.assertNotIn('path="/health"', messages)
            admissions = [line for line in logs.output if '[任务入队]' in line]
            self.assertEqual(len(admissions), 2)
            self.assertIn('queue_position=1', admissions[0])
            self.assertIn('queue_position=2', admissions[1])
            self.assertIn('ahead=1', admissions[1])
            updates = [line for line in logs.output if '[排队位置更新]' in line and second['id'] in line]
            self.assertTrue(any('queue_position=1' in line for line in updates))
            for job, admission in zip((first, second), admissions):
                request = re.search(r'request="([0-9a-f]+)"', admission)[1]
                worker_logs = [line for line in logs.output if job['id'] in line and any(
                    event in line for event in ('[任务开始]', '[执行步骤]', '[任务完成]'))]
                self.assertTrue(worker_logs)
                self.assertTrue(all('request="'+request+'"' in line for line in worker_logs))
            self.assertEqual(sum('[任务完成]' in line for line in logs.output), 2)
            self.assertIn('[队列空闲]', messages)
            self.assertIn('status=401', messages)
            self.assertIn('[发送媒体文件]', messages)

    def test_failed_job_reports_step_and_category_without_upstream_error_body(self):
        secret = 'upstream-cookie-and-url-secret'

        def parse(*args, **kwargs):
            step('正在读取平台数据')
            raise ParseError(secret, 'blocked')

        with tempfile.TemporaryDirectory() as folder:
            settings = Settings('test-token-at-least-24-characters', Path(folder))
            with self.assertLogs('yunx.api', level='INFO') as logs, \
                    patch('yunx_downloader.server.parse', side_effect=parse), \
                    TestClient(create_app(settings)) as client:
                headers = {'Authorization': 'Bearer '+settings.token}
                job = client.post('/v1/jobs', headers=headers,
                                  json={'text': 'https://x.com/test/status/2115039599554941098'}).json()
                for _ in range(100):
                    result = client.get(job['poll_url'], headers=headers).json()
                    if result['status'] == 'failed':
                        break
                    time.sleep(0.01)
                self.assertEqual(result['status'], 'failed')
            failure = next(line for line in logs.output if '[任务失败]' in line)
            self.assertIn('step="正在读取平台数据"', failure)
            self.assertIn('code="blocked"', failure)
            self.assertIn('elapsed_ms=', failure)
            self.assertNotIn(secret, '\n'.join(logs.output))

    def test_download_progress_and_failure_logs_omit_signed_url_and_headers(self):
        url = 'https://video.example.com/video.mp4?sig=private-download-signature'
        headers = {'Cookie': 'private-download-cookie'}
        session = Mock(spec=requests.Session)
        response = requests.Response()
        response.status_code = 200
        response.headers['Content-Length'] = '6'
        response.iter_content = lambda size: iter([b'123', b'456'])
        response._content_consumed = True
        session.get.return_value = response
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder)/'private-video-title.mp4'
            with self.assertLogs('yunx.api', level='INFO') as logs, bind_context(job='a'*32), \
                    patch('yunx_downloader.core.time.monotonic', side_effect=itertools.count(0, 6)):
                self.assertEqual(_download_file(session, url, target, headers, timeout=1), 6)
            messages = '\n'.join(logs.output)
            self.assertIn('[下载开始]', messages)
            self.assertIn('[下载进度]', messages)
            self.assertIn('percent=100', messages)
            self.assertIn('[下载完成]', messages)
            self.assertIn('bytes=6', messages)
            session.get.side_effect = requests.Timeout('private-exception-detail')
            with self.assertLogs('yunx.api', level='INFO') as failed_logs:
                with self.assertRaises(ParseError):
                    _download_file(session, url, target, headers, timeout=1)
            self.assertIn('[下载失败]', '\n'.join(failed_logs.output))
            messages += '\n'+'\n'.join(failed_logs.output)
            for secret in ('private-download-signature', 'private-download-cookie', 'private-video-title', 'private-exception-detail'):
                self.assertNotIn(secret, messages)
