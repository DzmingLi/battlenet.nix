#!/usr/bin/env python3
"""Resolve NGDP releases outside Nix evaluation; pin and materialize verified TACT content; authentication remains external."""
import argparse
import base64
import hashlib
from http.client import HTTPException
import json
import os
from pathlib import Path
import re
import tempfile
import time
from urllib.error import HTTPError
import urllib.request

RETRY_DELAYS = (0, 2, 5, 10, 20, 40, 60, 60)


def table(text):
    lines = [line for line in text.splitlines() if line and not line.startswith('#')]
    if not lines:
        raise ValueError('empty NGDP table')
    fields = [field.split('!')[0] for field in lines[0].split('|')]
    rows = []
    for line in lines[1:]:
        values = line.split('|')
        if len(values) != len(fields):
            raise ValueError('malformed NGDP table row')
        rows.append(dict(zip(fields, values)))
    return rows


def select(rows, field, value):
    matches = [row for row in rows if row.get(field) == value]
    if len(matches) != 1:
        raise ValueError(f'expected one {field}={value}, got {len(matches)}')
    return matches[0]


def retry_request(request):
    """Retry transport failures only; identity/format checks stay outside."""
    for attempt, delay in enumerate(RETRY_DELAYS):
        if delay:
            time.sleep(delay)
        try:
            return request()
        except (OSError, HTTPException) as error:
            if isinstance(error, HTTPError) and error.code not in (408, 429, 500, 502, 503, 504):
                raise
            if attempt == len(RETRY_DELAYS) - 1:
                raise


def cdn_url(url):
    """Prefer the global entry point while retaining advertised mirror URLs."""
    return re.sub(
        r'^https?://(?:(?:(?:us|eu|kr)\.)?cdn\.blizzard\.com|'
        r'level3(?:\.ssl)?\.blizzard\.com|blizzard\.gcdn\.cloudn\.co\.kr)(?=/|$)',
        'https://cdn.blizzard.com', url)


def get(url, max_size=None):
    def request():
        with urllib.request.urlopen(url, timeout=30) as response:
            length = response.headers.get('Content-Length')
            if max_size is not None and length and int(length) > max_size:
                raise ValueError('CDN object exceeds size limit')
            data = response.read() if max_size is None else response.read(max_size + 1)
        if max_size is not None and len(data) > max_size:
            raise ValueError('CDN object exceeds size limit')
        if length and len(data) < int(length):
            raise OSError('truncated CDN response')
        return data
    return retry_request(request)


def sri(data):
    return 'sha256-' + base64.b64encode(hashlib.sha256(data).digest()).decode()


def config_object(servers, path, key):
    if not re.fullmatch('[0-9a-f]{32}', key):
        raise ValueError(f'invalid config key: {key}')
    errors = []
    for server in servers:
        url = f'{server.rstrip("/")}/{path}/config/{key[:2]}/{key[2:4]}/{key}'
        try:
            data = get(url)
            if hashlib.md5(data).hexdigest() != key:
                raise ValueError('config MD5 differs from NGDP key')
            return {'key': key, 'url': url, 'hash': sri(data)}
        except (OSError, ValueError) as error:
            errors.append(f'{url}: {error}')
    raise ValueError('no verified config object available: ' + '; '.join(errors))


def resolve(product, region, endpoint):
    if not re.fullmatch('[a-z0-9_-]+', product):
        raise ValueError('product must be an NGDP product code')
    version = select(table(get(f'{endpoint}/{product}/versions').decode()), 'Region', region)
    cdn = select(table(get(f'{endpoint}/{product}/cdns').decode()), 'Name', region)
    # Servers carries HTTPS alternatives and routing query parameters. Strip the
    # query before appending the object path. Prefer the global entry point,
    # then retain every advertised mirror as a distinct fallback.
    from urllib.parse import urlsplit
    servers = []
    for value in cdn.get('Servers', '').split():
        parsed = urlsplit(value)
        if parsed.scheme in ('https', 'http') and parsed.netloc:
            servers.append(f'{parsed.scheme}://{parsed.netloc}')
    servers.sort(key=lambda value: not value.startswith('https:'))
    if not servers:
        servers = [f'http://{host}' for host in cdn['Hosts'].split()]
    servers = list(dict.fromkeys([cdn_url(server) for server in servers] + servers))
    return {
        'schemaVersion': 1, 'product': product, 'region': region,
        'buildId': version['BuildId'], 'version': version['VersionsName'],
        'buildConfig': config_object(servers, cdn['Path'], version['BuildConfig']),
        'cdnConfig': config_object(servers, cdn['Path'], version['CDNConfig']),
        'cdnPath': cdn['Path'], 'cdnServers': servers,
        'productConfig': version.get('ProductConfig', ''),
        'keyRing': version.get('KeyRing', ''),
    }


def verify_install(root, lock):
    marker = Path(root) / '.nix-casc.json'
    if marker.exists() and json.loads(marker.read_text()).get('completeInstallation') is not True:
        raise ValueError('partial CASC sample is not a playable installation')
    rows = table((Path(root) / '.build.info').read_text())
    matching = [r for r in rows if r.get('Build Key') == lock['buildConfig']['key']
                and r.get('CDN Key') == lock['cdnConfig']['key']
                and r.get('Product') == lock['product'] and r.get('Active') == '1'
                and r.get('Version') == lock['version']]
    if not matching:
        raise ValueError('active .build.info does not match locked product/build/CDN/version')
    # A snapshot must be self contained. External symlinks would defeat the
    # immutable content contract, and special files cannot be copied safely.
    for directory, dirs, files in os.walk(root):
        for name in dirs + files:
            path = Path(directory) / name
            if path.is_symlink() or not (path.is_dir() or path.is_file()):
                raise ValueError(f'snapshot contains a link or special file: {path}')


def atomic_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix='.' + path.name)
    try:
        with os.fdopen(descriptor, 'w') as stream:
            json.dump(data, stream, indent=2, sort_keys=True)
            stream.write('\n')
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    update = commands.add_parser('update', help='pin a release, including verified config SHA256s')
    update.add_argument('product', help='raw NGDP product; PTR/classic are separate products')
    update.add_argument('--region', default='us')
    update.add_argument('--endpoint', default='http://us.patch.battle.net:1119')
    update.add_argument('--output', required=True)
    verify = commands.add_parser('verify-install')
    verify.add_argument('root')
    verify.add_argument('lock')
    for name in ('prefetch-manifests', 'prefetch-indexes', 'prefetch-file', 'materialize-file', 'inspect-manifests', 'plan-install', 'resolve-plan', 'resolve-macos-launch'):
        command = commands.add_parser(name)
        command.add_argument('lock')
        command.add_argument('--cache', default=str(Path(os.environ.get('XDG_CACHE_HOME', str(Path.home() / '.cache'))) / 'battlenet-declarative' / 'objects'))
        if name != 'inspect-manifests':
            command.add_argument('--output', required=True)
        if name in ('plan-install', 'resolve-plan', 'prefetch-file', 'resolve-macos-launch'):
            command.add_argument('--tags', nargs='+', required=True, help='exact manifest tags; alternate tags retain base content')
        if name == 'prefetch-file':
            command.add_argument('--path', required=True)
        if name == 'resolve-macos-launch':
            command.add_argument('--application', required=True)
            command.add_argument('--pattern', default='')
    verify_manifests = commands.add_parser('verify-manifests')
    verify_manifests.add_argument('directory')
    verify_manifests.add_argument('lock')
    sample = commands.add_parser('prepare-storage-sample')
    sample.add_argument('lock')
    sample.add_argument('--cache', default=str(Path(os.environ.get('XDG_CACHE_HOME', str(Path.home() / '.cache'))) / 'battlenet-declarative' / 'objects'))
    sample.add_argument('--tags', nargs='+', required=True)
    sample.add_argument('--paths', nargs='+', required=True)
    sample.add_argument('--data-directory', default='Data')
    sample.add_argument('--output', required=True)
    full = commands.add_parser('prepare-installation', help='download and lock the complete selected closure; resumable')
    full.add_argument('lock')
    full.add_argument('--cache', required=True)
    full.add_argument('--tags', nargs='+', required=True)
    full.add_argument('--workers', type=int, choices=range(1, 17), default=8)
    full.add_argument('--data-directory', default='Data')
    full.add_argument('--install-directory', default='', help='subdirectory for loose files in a shared container')
    full.add_argument('--output', required=True)
    full.add_argument('--reuse-lock', action='append', default=[], help='reuse full-byte hashes from a pinned storage catalog')
    full.add_argument('--discard-cache', action='store_true', help='hash objects without retaining their encoded bytes')
    write_storage = commands.add_parser('materialize-storage')
    write_storage.add_argument('lock')
    write_storage.add_argument('--cache', required=True)
    write_storage.add_argument('--output', required=True)
    verification = commands.add_parser('validate-installation-lock', help='verify checkpoint completeness against pinned manifests without game payload downloads')
    verification.add_argument('lock')
    verification.add_argument('--cache', required=True)
    raw_object = commands.add_parser('materialize-encoded-object')
    raw_object.add_argument('lock')
    raw_object.add_argument('--output', required=True)
    installation = commands.add_parser('materialize-installation')
    installation.add_argument('lock')
    installation.add_argument('--output', required=True)
    installation.add_argument('--cache', help='assemble offline from already verified objects')
    args = parser.parse_args()
    try:
        if args.command == 'update':
            lock = resolve(args.product, args.region, args.endpoint.rstrip('/'))
            atomic_json(args.output, lock)
            print(f'{lock["product"]}: {lock["version"]} → {args.output}')
        elif args.command == 'verify-install':
            verify_install(args.root, json.loads(Path(args.lock).read_text()))
        else:
            import tact
            lock = json.loads(Path(args.lock).read_text())
            if args.command == 'prepare-installation':
                import full
                full.prepare(lock, args.cache, args.tags, args.output, args.workers, args.data_directory, args.discard_cache, args.install_directory, (json.loads(Path(path).read_text()) for path in args.reuse_lock))
            elif args.command == 'validate-installation-lock':
                import storage
                if not lock.get('completeInstallation'):
                    raise ValueError('checkpoint is not a complete installation')
                for name in ('buildConfig', 'cdnConfig'):
                    descriptor = lock['release'][name]
                    tact.atomic_bytes(Path(args.cache) / (descriptor['key'] + '.config'), tact.read_config(descriptor))
                storage.validate_closure(lock, args.cache)
                print('Complete pinned manifest closure verified; no game payloads downloaded')
            elif args.command == 'prepare-storage-sample':
                import storage
                result = storage.prepare_sample(lock, args.cache, args.tags, args.paths, args.data_directory)
                atomic_json(args.output, result)
                print(f'{len(result["objects"])} verified objects locked for an explicitly PARTIAL sample → {args.output}')
            elif args.command == 'materialize-storage':
                import storage
                storage.write_storage(lock, args.cache, args.output)
                print(f'CASC V7 storage (complete={lock.get("completeInstallation", False)}) → {args.output}')
            elif args.command == 'materialize-encoded-object':
                import storage
                tact.atomic_bytes(args.output, storage.fetch_locked(lock))
            elif args.command == 'materialize-installation':
                import storage
                storage.materialize_installation(lock, args.output, args.cache)
            elif args.command == 'prefetch-manifests':
                result = tact.prefetch(lock, args.cache)
                atomic_json(args.output, result)
                print(f'{lock["product"]}: locked encoding/install/download → {args.output}')
            elif args.command == 'prefetch-indexes':
                result = tact.prefetch_indexes(lock, args.cache)
                atomic_json(args.output, result)
                print(f'{len(result["archives"])} verified archive indexes → {args.output}')
            elif args.command == 'prefetch-file':
                result = tact.prefetch_file(lock, args.cache, args.path, args.tags)
                atomic_json(args.output, result)
                print(f'{result["path"]}: {result["decodedSize"]} verified bytes → {args.output}')
            elif args.command == 'resolve-macos-launch':
                atomic_json(args.output, tact.macos_launch(lock, args.cache, args.tags, args.application, args.pattern))
            elif args.command == 'materialize-file':
                tact.materialize_file(lock, args.output)
            elif args.command == 'resolve-plan':
                result = tact.resolve_plan(lock, args.cache, args.tags)
                atomic_json(args.output, result)
                print(f'{len(result["downloadObjects"])} CASC objects and {len(result["installFiles"])} install files have verified locations')
            elif args.command == 'plan-install':
                result = tact.make_plan(lock, args.cache, args.tags)
                atomic_json(args.output, result)
                print(f'{len(result["installFiles"])} loose files, {len(result["downloadObjects"])} CASC objects, {result["totalEncodedBytes"]} encoded bytes')
            elif args.command == 'verify-manifests':
                tact.verify_manifest_directory(args.directory, lock)
            else:
                result = tact.inspect(lock, args.cache)
                print(json.dumps(result, indent=2, sort_keys=True))
    except (OSError, ValueError, KeyError) as error:
        parser.exit(1, f'error: {error}\n')


if __name__ == '__main__':
    main()
