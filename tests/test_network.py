"""Transport regressions: retry budgets, mirror identity and bounded ranges."""
import hashlib
from http.client import RemoteDisconnected
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

import ngdp
import full
import tact


class NetworkTests(unittest.TestCase):
    def test_transport_recovers_beyond_previous_retry_budget(self):
        with patch('ngdp.time.sleep') as sleep:
            with patch('ngdp.urllib.request.urlopen', side_effect=[
                RemoteDisconnected('connection closed') for _ in range(5)
            ] + [self.response(b'verified')]) as request:
                self.assertEqual(ngdp.get('https://example.test/object'), b'verified')
        self.assertEqual(request.call_count, 6)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [2, 5, 10, 20, 40])

    def test_transient_http_failure_exhausts_eight_attempts(self):
        error = HTTPError('https://example.test/object', 503, 'unavailable', {}, None)
        self.addCleanup(error.close)
        with patch('ngdp.time.sleep') as sleep:
            with patch('ngdp.urllib.request.urlopen', side_effect=error) as request:
                with self.assertRaises(HTTPError):
                    ngdp.get(error.url)
        self.assertEqual(request.call_count, 8)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [2, 5, 10, 20, 40, 60, 60])

    def test_permanent_http_and_validation_errors_do_not_retry(self):
        for error in [HTTPError('https://example.test', 404, 'missing', {}, None),
                      ValueError('invalid content')]:
            if isinstance(error, HTTPError):
                self.addCleanup(error.close)
            with self.subTest(error=error), patch('ngdp.time.sleep') as sleep:
                with patch('ngdp.urllib.request.urlopen', side_effect=error) as request:
                    with self.assertRaises(type(error)):
                        ngdp.get('https://example.test')
                self.assertEqual(request.call_count, 1)
                sleep.assert_not_called()

    @staticmethod
    def response(data, **headers):
        response = io.BytesIO(data)
        response.headers = headers
        response.status = 206 if 'Content-Range' in headers else 200
        return response

    def test_selected_backup_hostname_is_not_rewritten(self):
        url = 'https://eu.cdn.blizzard.com/tpr/wow/config/key'
        with patch('ngdp.urllib.request.urlopen', return_value=self.response(b'config')) as request:
            self.assertEqual(ngdp.get(url), b'config')
        self.assertEqual(request.call_args.args[0], url)

    def test_range_retry_preserves_backup_host_and_exact_byte_range(self):
        url = 'https://level3.ssl.blizzard.com/tpr/wow/data/archive'
        response = self.response(b'abc', **{'Content-Length': '3', 'Content-Range': 'bytes 10-12/100'})
        with patch('ngdp.time.sleep'):
            with patch('urllib.request.urlopen', side_effect=[OSError('offline'), response]) as request:
                self.assertEqual(tact.bounded_get(url, 3, 10, 3), b'abc')
        for call in request.call_args_list:
            self.assertEqual(call.args[0].full_url, url)
            self.assertEqual(call.args[0].get_header('Range'), 'bytes=10-12')

    def test_resolve_retains_and_uses_official_mirrors(self):
        config = b'archives = test\n'
        key = hashlib.md5(config).hexdigest()
        servers = ['https://us.cdn.blizzard.com', 'https://eu.cdn.blizzard.com',
                   'http://level3.blizzard.com']
        calls = []

        def get(url):
            calls.append(url)
            if url.endswith('/versions'):
                return f'Region|BuildConfig|CDNConfig|BuildId|VersionsName\nus|{key}|{key}|1|1.0\n'.encode()
            if url.endswith('/cdns'):
                return ('Name|Path|Hosts|Servers\nus|tpr/wow|unused|' +
                        ' '.join(server + '?route=test' for server in servers) + '\n').encode()
            if url.startswith('https://eu.cdn.blizzard.com/'):
                return config
            error = HTTPError(url, 404, 'missing on this mirror', {}, None)
            self.addCleanup(error.close)
            raise error

        with patch('ngdp.get', side_effect=get):
            release = ngdp.resolve('wow', 'us', 'https://patch.test')
        self.assertEqual(release['cdnServers'], ['https://cdn.blizzard.com'] + servers)
        self.assertTrue(release['buildConfig']['url'].startswith(servers[1] + '/'))
        self.assertTrue(release['cdnConfig']['url'].startswith(servers[1] + '/'))
        self.assertEqual(release['buildConfig']['hash'], ngdp.sri(config))
        self.assertTrue(any(url.startswith(servers[0] + '/') for url in calls))

    def test_complete_installation_can_use_third_advertised_server(self):
        payload = b'verified payload'
        encoded = b'BLTE' + b'\0' * 4 + b'N' + payload
        ekey, ckey = (hashlib.md5(data).hexdigest() for data in (encoded, payload))
        servers = ['https://first.test', 'https://second.test', 'https://third.test']
        bootstrap = dict(release=dict(cdnServers=servers, cdnPath='tpr/wow', buildConfig=dict(key='a' * 32)))
        plan = dict(downloadObjects=[dict(ekey=ekey, size=len(encoded), location=dict(size=len(encoded)))], installFiles=[])

        def fetch(url, *args):
            if url.startswith(servers[2] + '/'):
                return encoded
            raise OSError('mirror unavailable')

        with (
            tempfile.TemporaryDirectory() as directory,
            patch('full.storage.prepare_sample', return_value=dict(objects=[])),
            patch('full.tact.resolve_plan', return_value=plan),
            patch('full.tact.load_manifests', return_value=dict(encoding=b'')),
            patch('full.tact.encoding', return_value={ckey: dict(ekeys=[ekey], size=len(payload))}),
            patch('full.tact.bounded_get', side_effect=fetch) as request,
            patch('full.storage.validate_closure'),
            patch('builtins.print'),
        ):
            output = Path(directory) / 'storage.json'
            full.prepare(bootstrap, directory, [], output, workers=1)
            result = json.loads(output.read_text())
        self.assertEqual(request.call_count, 3)
        self.assertTrue(result['objects'][0]['source']['url'].startswith(servers[2] + '/'))
        self.assertEqual(result['objects'][0]['hash'], ngdp.sri(encoded))


if __name__ == '__main__':
    unittest.main()
