"""Updater regressions: retry dependencies, optional reuse and release publication."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
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

    def nu(self, expression, success=True):
        result = subprocess.run(
            ['nu', '--no-config-file', '-c', 'use ./update.nu *; ' + expression],
            cwd=ROOT, env=self.env, text=True, capture_output=True,
        )
        if success:
            self.assertEqual(result.returncode, 0, result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0)
        return result

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
if attempt == 1:
    print('HTTP error 404', file=sys.stderr)
    sys.exit(1)
print('/nix/store/verified-catalog')
''')
        result = self.nu('upload-catalog archive.tar.gz https://example.test/catalog sha256-test owner/repo')
        self.assertEqual(result.stdout.strip(), '/nix/store/verified-catalog')
        self.assertEqual(counter.read_text(), '2')

    def test_duplicate_upload_requires_successful_hash_verification(self):
        self.command('gh', 'import sys\nprint("already exists", file=sys.stderr)\nsys.exit(1)\n')
        self.command('nix', 'print("/nix/store/verified-catalog")\n')
        result = self.nu('upload-catalog archive.tar.gz https://example.test/catalog sha256-test owner/repo')
        self.assertEqual(result.stdout.strip(), '/nix/store/verified-catalog')

    def test_publication_failure_reports_upload_and_verification_errors(self):
        self.command('gh', 'import sys\nprint("upload denied", file=sys.stderr)\nsys.exit(1)\n')
        self.command('nix', 'import sys\nprint("hash mismatch", file=sys.stderr)\nsys.exit(1)\n')
        result = self.nu('upload-catalog archive.tar.gz https://example.test/catalog sha256-test owner/repo', success=False)
        self.assertIn('upload denied', result.stderr)
        self.assertIn('hash mismatch', result.stderr)
        self.assertIn('four attempts', result.stderr)


if __name__ == '__main__':
    unittest.main()
