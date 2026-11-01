"""Updater regressions: retry dependencies, optional reuse and release publication."""
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest

ROOT = Path(__file__).resolve().parents[1]


class UpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name)
        self.bin = self.work / 'bin'
        self.bin.mkdir()
        self.env = dict(os.environ, PATH=str(self.bin) + os.pathsep + os.environ['PATH'])

    def command(self, name, body):
        script = self.bin / name
        script.write_text('#!/usr/bin/env python3\n' + body)
        script.chmod(0o755)

    def nu(self, expression, success=True, fast_retries=False):
        module = ROOT / 'update.nu'
        if fast_retries:
            # Exercise the real retry/verification code without wall-clock waits.
            module = self.work / 'update.nu'
            source = (ROOT / 'update.nu').read_text()
            line = next(line for line in source.splitlines() if line.startswith('const RETRY_DELAYS = '))
            delays = line.split('[', 1)[1].split(']', 1)[0].split()
            module.write_text(source.replace(line, 'const RETRY_DELAYS = [' + ' '.join('0sec' for _ in delays) + ']'))
        result = subprocess.run(
            ['nu', '--no-config-file', '-c', f'use {json.dumps(str(module))} *; ' + expression],
            cwd=ROOT, env=self.env, text=True, capture_output=True,
        )
        if success:
            self.assertEqual(result.returncode, 0, result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0)
        return result

    def server(self, route):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                status, body = route(self.path)
                self.send_response(status)
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join)
        self.addCleanup(server.shutdown)
        return f'http://127.0.0.1:{server.server_port}'

    def test_cdn_servers_keep_official_alternatives_and_china_hosts(self):
        servers = ['https://us.cdn.blizzard.com', 'https://eu.cdn.blizzard.com',
                   'http://level3.blizzard.com', 'https://blzdist-hs.necdn.leihuo.netease.com']
        result = self.nu(f'cdn-servers ({json.dumps(servers)!r} | from json) | to json --raw')
        self.assertEqual(json.loads(result.stdout), ['https://cdn.blizzard.com', servers[-1]] + servers[:3])

    def test_discovery_recovers_after_five_failures_and_uses_backup(self):
        config = b'archives = test\n'
        key = hashlib.md5(config).hexdigest()
        counts = dict(versions=0, primary=0, backup=0)

        def route(path):
            if path.endswith('/versions'):
                counts['versions'] += 1
                if counts['versions'] <= 5:
                    return 503, b'temporarily unavailable'
                return 200, f'Region|BuildConfig|CDNConfig|BuildId|VersionsName\nus|{key}|{key}|1|1.0\n'.encode()
            if path.endswith('/cdns'):
                return 200, f'Name|Path|Hosts|Servers\nus|tpr/wow|unused|{endpoint}/primary {endpoint}/backup\n'.encode()
            if path.startswith('/primary/'):
                counts['primary'] += 1
                return 404, b'missing on this mirror'
            counts['backup'] += 1
            return 200, config

        endpoint = self.server(route)
        result = self.nu(f'resolve-release wow us {endpoint} | to json --raw', fast_retries=True)
        release = json.loads(result.stdout)
        self.assertEqual(counts, dict(versions=6, primary=2, backup=2))
        self.assertEqual(release['cdnServers'], [endpoint + '/primary', endpoint + '/backup'])
        self.assertTrue(release['buildConfig']['url'].startswith(endpoint + '/backup/'))

    def test_discovery_exhausts_transient_errors_but_not_permanent_errors(self):
        counts = {503: 0, 404: 0}

        def route(path):
            status = 503 if '/transient/' in path else 404
            counts[status] += 1
            return status, b'unavailable'

        endpoint = self.server(route)
        transient = self.nu(f'resolve-release transient us {endpoint}', success=False, fast_retries=True)
        self.nu(f'resolve-release permanent us {endpoint}', success=False, fast_retries=True)
        self.assertEqual(counts, {503: 8, 404: 1})
        self.assertIn('after 8 attempts', transient.stderr)

    def test_cleanup_protects_recent_completed_and_old_active_runs(self):
        runs = [
            dict(id=1, head_sha='old', status='completed', created_at='2026-09-20T00:00:00Z'),
            dict(id=2, head_sha='retry', status='completed', created_at='2026-09-21T00:00:00Z'),
            dict(id=3, head_sha='active', status='in_progress', created_at='2026-09-01T00:00:00Z'),
            dict(id=4, head_sha='own', status='in_progress', created_at='2026-10-21T00:00:00Z'),
        ]
        self.env['GITHUB_RUN_ID'] = '4'
        source = self.work / 'runs.json'
        source.write_text(json.dumps(runs))
        result = self.nu(f'catalog-retained-runs (open {source}) 2026-09-21T00:00:00Z | to json --raw')
        self.assertEqual([r['id'] for r in json.loads(result.stdout)], [2, 3, 4])

    def test_cleanup_preserves_references_and_recent_uncommitted_uploads(self):
        old, referenced, recent = [char * 64 + '.tar.gz' for char in 'abc']
        assets = [dict(name=name, state='uploaded', created_at=created) for name, created in [
            (old, '2026-09-01T00:00:00Z'), (referenced, '2026-09-01T00:00:00Z'),
            (recent, '2026-10-20T00:00:00Z'), ('other.zip', '2026-09-01T00:00:00Z'),
        ]]
        source = self.work / 'assets.json'
        source.write_text(json.dumps(assets))
        result = self.nu(f'catalog-prune-plan (open {source}) ["{referenced}"] 2026-09-21T00:00:00Z | to json --raw')
        self.assertEqual([a['name'] for a in json.loads(result.stdout)], [old])

    def test_missing_reuse_catalog_falls_back_without_writing_lock(self):
        self.command('nix', 'import sys\nprint("HTTP error 404", file=sys.stderr)\nsys.exit(1)\n')
        output = self.work / 'reuse.json'
        result = self.nu(f'reuse-catalog missing.nix {output} | to json --raw')
        self.assertEqual(json.loads(result.stdout), [])
        self.assertFalse(output.exists())
        self.assertIn('downloading and verifying objects instead', result.stderr)
        self.assertIn('HTTP error 404', result.stderr)

    def test_cleanup_dry_run_preserves_retry_references_end_to_end(self):
        main, retry, obsolete = [char * 64 + '.tar.gz' for char in 'abc']
        runs = [dict(workflow_runs=[dict(id=42, head_sha='retry-sha',
                    status='completed', created_at='2050-01-01T00:00:00Z')])]
        assets = [[dict(name=name, state='uploaded', created_at='2000-01-01T00:00:00Z', size=100)
                   for name in [main, retry, obsolete]]]
        self.command('gh', f'''import json, sys
args = ' '.join(sys.argv[1:])
if '/actions/runs?' in args:
    print({json.dumps(runs)!r})
elif '/releases/tags/' in args:
    print('{{"id": 1}}')
elif '/assets?' in args:
    print({json.dumps(assets)!r})
else:
    sys.exit('unexpected gh call: ' + args)
''')
        self.command('git', f'''import sys
args = sys.argv[1:]
if args[0] == 'rev-parse':
    print('main-sha')
elif args[0] == 'grep':
    asset = {retry!r} if 'retry-sha' in args else {main!r}
    print('https://github.com/owner/repo/releases/download/catalogs/' + asset)
elif args[0] not in ('fetch', 'cat-file'):
    sys.exit('unexpected git call')
''')
        result = self.nu('prune-catalogs --repository owner/repo')
        self.assertIn('protected references: 2; obsolete: 1', result.stdout)
        self.assertIn('Would delete ' + obsolete, result.stdout)
        self.assertNotIn('Would delete ' + retry, result.stdout)

    def test_valid_reuse_catalog_keeps_object_hashes(self):
        objects = dict(objects=[dict(ekey='test', hash='sha256-pinned')])
        self.command('nix', 'print(' + repr(json.dumps(objects)) + ')\n')
        output = self.work / 'reuse.json'
        result = self.nu(f'reuse-catalog existing.nix {output} | to json --raw')
        self.assertEqual(json.loads(result.stdout), ['--reuse-lock', str(output)])
        self.assertEqual(json.loads(output.read_text()), objects)

    def test_publication_retries_unavailable_download(self):
        counter = self.work / 'attempts'
        self.command('gh', 'pass\n')
        self.command('nix', f'''import pathlib, sys
counter = pathlib.Path({str(counter)!r})
attempt = int(counter.read_text()) + 1 if counter.exists() else 1
counter.write_text(str(attempt))
if attempt <= 5:
    print('HTTP error 404', file=sys.stderr)
    sys.exit(1)
print('/nix/store/verified-catalog')
''')
        result = self.nu('upload-catalog archive.tar.gz https://example.test/catalog sha256-test owner/repo', fast_retries=True)
        self.assertEqual(result.stdout.strip(), '/nix/store/verified-catalog')
        self.assertEqual(counter.read_text(), '6')

    def test_duplicate_upload_requires_successful_hash_verification(self):
        self.command('gh', 'import sys\nprint("already exists", file=sys.stderr)\nsys.exit(1)\n')
        self.command('nix', 'print("/nix/store/verified-catalog")\n')
        result = self.nu('upload-catalog archive.tar.gz https://example.test/catalog sha256-test owner/repo')
        self.assertEqual(result.stdout.strip(), '/nix/store/verified-catalog')

    def test_publication_failure_reports_upload_and_verification_errors(self):
        self.command('gh', 'import sys\nprint("upload denied", file=sys.stderr)\nsys.exit(1)\n')
        self.command('nix', 'import sys\nprint("hash mismatch", file=sys.stderr)\nsys.exit(1)\n')
        result = self.nu('upload-catalog archive.tar.gz https://example.test/catalog sha256-test owner/repo', success=False, fast_retries=True)
        self.assertIn('upload denied', result.stderr)
        self.assertIn('hash mismatch', result.stderr)
        self.assertIn('after 8 attempts', result.stderr)


if __name__ == '__main__':
    unittest.main()
