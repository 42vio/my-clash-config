import json
import os
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from clash_sub.software_updates import (
    CACHE_NAME, CHECK_INTERVAL, SoftwareUpdates, query_release, read_running_versions, version_tuple,
)


class SoftwareUpdateTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.now = [1793581200]
        self.reader = Mock(side_effect=lambda key: {'xui': 'v3.11.0', 'xray': 'v26.10.31'}[key])
        self.running = Mock(return_value={'xui': '3.10.0', 'xray': '26.10.21'})

    def cache(self):
        return SoftwareUpdates(self.root, clock=lambda: self.now[0], release_reader=self.reader, running_reader=self.running)

    def settle(self, cache):
        cache.get()
        cache.thread.join(timeout=3)
        self.assertFalse(cache.thread.is_alive())
        return cache.get()

    def test_numeric_versions_are_compared_numerically(self):
        self.assertGreater(version_tuple('v3.11.0'), version_tuple('3.9.99'))
        self.assertGreater(version_tuple('26.10.31'), version_tuple('26.9.30'))
        for bad in ['latest', '3.11.0-rc1', '3.11.0<script>', '', None]:
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                version_tuple(bad)

    def test_get_is_nonblocking_and_concurrent_requests_share_one_check(self):
        entered, release = threading.Event(), threading.Event()
        original = self.reader.side_effect
        def reader(key):
            entered.set()
            release.wait(timeout=3)
            return original(key)
        self.reader.side_effect = reader
        cache = self.cache()
        before = time.monotonic()
        result = cache.get()
        self.assertLess(time.monotonic() - before, .5)
        self.assertTrue(result['checking'])
        self.assertTrue(entered.wait(timeout=1))
        try:
            for _ in range(10):
                self.assertTrue(cache.get()['checking'])
            self.assertEqual(self.reader.call_count, 1)
        finally:
            release.set()
            cache.thread.join(timeout=3)
        self.assertEqual(self.reader.call_count, 2)
        self.assertEqual(cache.get()['current']['xray'], '26.10.21')

    def test_daily_results_persist_across_restart_but_running_versions_refresh(self):
        cache = self.cache()
        result = self.settle(cache)
        self.assertEqual(result['releases']['xui']['version'], 'v3.11.0')
        self.assertEqual((self.root / CACHE_NAME).stat().st_mode & 0o777, 0o600)
        restarted = self.cache()
        self.running.return_value = {'xui': '3.11.0', 'xray': '26.10.31'}
        result = self.settle(restarted)
        self.assertEqual(self.reader.call_count, 2)
        self.assertEqual(result['current']['xray'], '26.10.31')
        self.now[0] += 60
        self.settle(restarted)
        self.assertEqual(self.reader.call_count, 2)
        self.now[0] += CHECK_INTERVAL
        self.settle(restarted)
        self.assertEqual(self.reader.call_count, 4)

    def test_one_failed_project_preserves_last_success_and_failure_across_restart(self):
        cache = self.cache()
        self.settle(cache)
        self.now[0] += CHECK_INTERVAL
        def reader(key):
            if key == 'xui':
                raise ValueError('private upstream error')
            return 'v26.11.1'
        self.reader.side_effect = reader
        result = self.settle(cache)
        self.assertEqual(result['failed'], ['xui'])
        self.assertEqual(result['releases']['xui']['version'], 'v3.11.0')
        self.assertEqual(result['releases']['xray']['version'], 'v26.11.1')
        saved = (self.root / CACHE_NAME).read_text()
        self.assertNotIn('private upstream error', saved)
        calls = self.reader.call_count
        self.assertEqual(self.settle(self.cache())['failed'], ['xui'])
        self.assertEqual(self.reader.call_count, calls)

    def test_runtime_read_failure_does_not_keep_a_stale_running_version(self):
        cache = self.cache()
        self.settle(cache)
        self.now[0] += 60
        self.running.side_effect = OSError('unavailable')
        result = self.settle(cache)
        self.assertEqual(result['current'], {'xui': None, 'xray': None})
        self.assertEqual(self.reader.call_count, 2)

    def test_unsafe_cache_is_not_followed_or_overwritten(self):
        outside = self.root / 'outside'
        outside.write_text('unchanged')
        (self.root / CACHE_NAME).symlink_to(outside)
        with self.assertLogs('clash_sub.software_updates', level='WARNING') as logs:
            result = self.settle(self.cache())
        self.assertEqual(outside.read_text(), 'unchanged')
        self.assertEqual(result['failed'], [])
        self.assertIn('software_update_cache_write_failed', ''.join(logs.output))

    def test_release_queries_use_fixed_public_host_and_ignore_arbitrary_html_url(self):
        response = Mock(status=200)
        response.read.return_value = json.dumps({'tag_name':'v3.11.0','draft':False,'prerelease':False,
                                                'html_url':'https://untrusted.invalid/'}).encode()
        connection = Mock()
        connection.getresponse.return_value = response
        factory = Mock(return_value=connection)
        self.assertEqual(query_release('xui', connection_factory=factory), 'v3.11.0')
        factory.assert_called_once_with('api.github.com', 443, timeout=5)
        args, kwargs = connection.request.call_args
        self.assertEqual(args, ('GET', '/repos/MHSanaei/3x-ui/releases/latest'))
        self.assertNotIn('Authorization', kwargs['headers'])
        connection.close.assert_called_once()
        for changes in [{'prerelease':True}, {'draft':True}, {'tag_name':'v3.11.0-rc1'}]:
            data = {'tag_name':'v3.11.0','draft':False,'prerelease':False, **changes}
            response.read.return_value = json.dumps(data).encode()
            with self.assertRaisesRegex(ValueError, 'software_release_check_failed'):
                query_release('xui', connection_factory=factory)
        response.status = 302
        with self.assertRaises(ValueError):
            query_release('xui', connection_factory=factory)

    def test_live_executables_are_used_instead_of_installed_files(self):
        parent, child = self.root/'111', self.root/'222'
        (parent/'task'/'111').mkdir(parents=True)
        child.mkdir()
        (parent/'exe').symlink_to('/usr/local/x-ui/x-ui (deleted)')
        (child/'exe').symlink_to('/usr/local/x-ui/bin/xray-linux-amd64 (deleted)')
        (parent/'task'/'111'/'children').write_text('')
        (parent/'task'/'333').mkdir()
        (parent/'task'/'333'/'children').write_text('222')
        def runner(argv):
            if argv[0] == '/usr/bin/systemctl': return '111'
            if argv == [str(parent/'exe'), '-v']: return '3.11.0'
            if argv == [str(child/'exe'), 'version']: return 'Xray 26.10.21 (Xray, Penetrates Everything.)'
            self.fail('Unexpected executable')
        result = read_running_versions(proc_root=self.root, runner=runner)
        self.assertEqual(result, {'xui':'3.11.0','xray':'26.10.21'})
        (parent/'task'/'333'/'children').write_text('')
        self.assertIsNone(read_running_versions(proc_root=self.root, runner=runner)['xray'])

    def test_absent_service_is_unknown_and_never_uses_installed_binaries(self):
        runner = Mock(return_value='0')
        self.assertEqual(read_running_versions(proc_root=self.root, runner=runner), {'xui':None,'xray':None})
        self.assertEqual(runner.call_count, 1)
        runner.side_effect = subprocess.TimeoutExpired('systemctl', 1)
        self.assertEqual(read_running_versions(proc_root=self.root, runner=runner), {'xui':None,'xray':None})


if __name__ == '__main__':
    unittest.main()
