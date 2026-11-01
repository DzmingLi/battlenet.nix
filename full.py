"""Resumable full manifest prefetch; bounded coalesced CDN ranges."""
import argparse
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
import json
from pathlib import Path
import time

import storage
import tact
from ngdp import atomic_json, sri


def reuse_cached(item, cache, release):
    """Reuse content across build/channel changes, with the new locked location."""
    raw = (Path(cache) / item['ekey']).read_bytes()
    decoded = tact.storage_payload(raw, item['ekey'], item['ckey'], item['decodedSize'])
    location = item['location']
    if len(raw) != location['size'] or (decoded is not None and len(decoded) != item['decodedSize']):
        raise ValueError('cached object size mismatch')
    key = location.get('archive', item['ekey'])
    url = f'{release["cdnServers"][0].rstrip("/")}/{release["cdnPath"]}/data/{key[:2]}/{key[2:4]}/{key}'
    return {'ekey': item['ekey'], 'ckey': item['ckey'], 'hash': sri(raw),
            'encodedSize': len(raw), 'decodedSize': item['decodedSize'],
            'source': dict(location, url=url, kind='archive' if 'archive' in location else 'loose'),
            **({'encrypted': True, 'decodedVerified': False} if decoded is None else {})}


def groups(items, limit=24 * 1024 * 1024, gap=32768):
    archives = {}
    result = []
    for item in items:
        location = item['location']
        if 'archive' not in location:
            result.append([item])
        else:
            archives.setdefault(location['archive'], []).append(item)
    for items in archives.values():
        current = []
        end = 0
        for item in sorted(items, key=lambda x: x['location']['offset']):
            loc = item['location']
            if current and (loc['offset'] - end > gap or loc['offset'] + loc['size'] - current[0]['location']['offset'] > limit):
                result.append(current)
                current = []
            current.append(item)
            end = loc['offset'] + loc['size']
        if current:
            result.append(current)
    return result


def prepare(bootstrap, cache, names, output, workers=8, data_directory='Data', discard_cache=False, install_directory='', reuse_locks=()):
    cache = Path(cache)
    # This also locks every root/VFS object required by the build config.
    seed = storage.prepare_sample(bootstrap, cache, names, [], data_directory, max_bytes=512 * 1024 * 1024)
    plan = tact.resolve_plan(bootstrap, cache, names)
    encoding = tact.encoding(tact.load_manifests(bootstrap, cache)['encoding'])
    reverse = {}
    for ckey, value in encoding.items():
        for ekey in value['ekeys']:
            if ekey in reverse and reverse[ekey] != (ckey, value['size']):
                raise ValueError('ambiguous encoding EKey')
            reverse[ekey] = (ckey, value['size'])
    pending = {e['ekey']: dict(e) for e in plan['downloadObjects']}
    for file in plan['installFiles']:
        pending.setdefault(file['ekey'], {'ekey': file['ekey'], 'size': file['location']['size'], 'location': file['location']})
    for item in pending.values():
        item['ckey'], item['decodedSize'] = reverse[item['ekey']]
    release = bootstrap['release']
    journal = cache / (release['buildConfig']['key'] + '-full-journal.jsonl')
    completed = {e['ekey']: e for e in seed['objects']}
    if journal.exists():
        for line in journal.read_text().splitlines():
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue  # A killed writer may leave an incomplete last line.
            if item['ekey'] in pending:
                try:
                    raw = (cache / item['ekey']).read_bytes()
                    wanted = pending[item['ekey']]
                    if (len(raw) == wanted['size'] and sri(raw) == item['hash']
                            and item['ckey'] == wanted['ckey'] and item['decodedSize'] == wanted['decodedSize']):
                        completed[item['ekey']] = item
                except OSError:
                    pass
    # A new build gets a different journal, but most EKeys may be unchanged.
    # Revalidate cached bytes rather than redownloading the whole game.
    with journal.open('a') as ledger:
        ledger.write('\n')  # Separate a potentially truncated last JSON line.
        reused = 0
        for key, item in pending.items():
            if key not in completed and (cache / key).exists():
                try:
                    descriptor = reuse_cached(item, cache, release)
                except (OSError, ValueError):
                    continue  # A damaged cache entry is replaced by verified CDN bytes.
                completed[key] = descriptor
                ledger.write(json.dumps(descriptor, sort_keys=True) + '\n')
                reused += 1
        if reused:
            print(f'Revalidated {reused} cached objects across builds', flush=True)
    # Trust only previously pinned full-byte SHA-256 records, never EKey alone.
    reused_hashes = 0
    for lock in reuse_locks:
        for old in lock.get('objects', []):
            key = old['ekey']
            wanted = pending.get(key)
            if key in completed or wanted is None:
                continue
            if (old['ckey'] != wanted['ckey'] or old['decodedSize'] != wanted['decodedSize']
                    or old['encodedSize'] != wanted['size']):
                raise ValueError('conflicting pinned object identity: ' + key)
            import base64
            if not old['hash'].startswith('sha256-') or len(base64.b64decode(old['hash'][7:], validate=True)) != 32:
                raise ValueError('invalid pinned object SHA-256: ' + key)
            loc = wanted['location']
            source_key = loc.get('archive', key)
            url = f'{release["cdnServers"][0].rstrip("/")}/{release["cdnPath"]}/data/{source_key[:2]}/{source_key[2:4]}/{source_key}'
            completed[key] = dict(old, source=dict(loc, url=url,
                kind='archive' if 'archive' in loc else 'loose'))
            reused_hashes += 1
    if reused_hashes:
        print(f'Reused {reused_hashes} pinned object hashes without downloading payloads', flush=True)
    jobs = groups([e for key, e in pending.items() if key not in completed])
    total = sum(e['size'] for e in pending.values())
    done = sum(e['size'] for key, e in pending.items() if key in completed)
    print(f'Closure: {len(pending)} objects, {total / 2**30:.2f} GiB; cached {done / 2**30:.2f} GiB; {len(jobs)} range requests', flush=True)

    def fetch(batch):
        first, last = batch[0]['location'], batch[-1]['location']
        offset = first.get('offset')
        size = last['offset'] + last['size'] - offset if offset is not None else first['size']
        key = first.get('archive', batch[0]['ekey'])
        errors = []
        for attempt in range(3):
            for server in release['cdnServers']:
                url = f'{server.rstrip("/")}/{release["cdnPath"]}/data/{key[:2]}/{key[2:4]}/{key}'
                try:
                    raw = tact.bounded_get(url, size, offset, size)
                    descriptors = []
                    for item in batch:
                        loc = item['location']
                        start = loc['offset'] - offset if offset is not None else 0
                        encoded = raw[start:start + loc['size']]
                        decoded = tact.storage_payload(encoded, item['ekey'], item['ckey'], item['decodedSize'])
                        if (decoded is not None and len(decoded) != item['decodedSize']):
                            raise ValueError('decoded size mismatch')
                        if not discard_cache:
                            tact.atomic_bytes(cache / item['ekey'], encoded)
                        descriptors.append({'ekey': item['ekey'], 'ckey': item['ckey'], 'hash': sri(encoded),
                            'encodedSize': len(encoded), 'decodedSize': item['decodedSize'],
                            'source': dict(loc, url=url, kind='archive' if offset is not None else 'loose'),
                            **({'encrypted': True, 'decodedVerified': False} if decoded is None else {})})
                    return descriptors
                except (OSError, ValueError) as error:
                    # An unsupported codec cannot be fixed by retrying a mirror.
                    if 'unsupported' in str(error) or 'encrypted' in str(error):
                        raise
                    errors.append(str(error))
            time.sleep(attempt + 1)
        raise ValueError(key + ': ' + '; '.join(errors))

    started = last_report = time.monotonic()
    initial = done
    with journal.open('a') as ledger, ThreadPoolExecutor(max_workers=workers) as pool:
        batches = iter(jobs)
        futures = set()
        def submit_next():
            batch = next(batches, None)
            if batch is not None:
                futures.add(pool.submit(fetch, batch))
        for _ in range(workers * 2):
            submit_next()
        try:
            while futures:
                ready, _ = wait(futures, return_when=FIRST_COMPLETED)
                for future in ready:
                    futures.remove(future)
                    for item in future.result():
                        completed[item['ekey']] = item
                        done += item['encodedSize']
                        ledger.write(json.dumps(item, sort_keys=True) + '\n')
                    submit_next()
                ledger.flush()
                now = time.monotonic()
                if now - last_report >= 10:
                    speed = (done - initial) / (now - started) / 2**20
                    print(f'{done / 2**30:.2f}/{total / 2**30:.2f} GiB ({done / total:.1%}), {len(completed)} objects, {speed:.1f} MiB/s', flush=True)
                    last_report = now
        except BaseException:
            for future in futures:
                future.cancel()
            raise
    result = dict(seed, writerVersion=2, completeInstallation=True, bootstrap=bootstrap,
        objects=[completed[key] for key in sorted(completed)],
        installFiles=[{k: e[k] for k in ('path', 'ckey', 'ekey', 'size')} for e in plan['installFiles']])
    if install_directory:
        result['installDirectory'] = tact.safe_path(install_directory)
    storage.validate_closure(result, cache)
    atomic_json(output, result)
    print(f'Complete verified lock: {output}', flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('bootstrap')
    parser.add_argument('--cache', required=True)
    parser.add_argument('--tags', nargs='+', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--workers', type=int, default=8)
    parser.add_argument('--data-directory', default='Data')
    args = parser.parse_args()
    if not 1 <= args.workers <= 16:
        parser.error('workers must be between 1 and 16')
    prepare(json.loads(Path(args.bootstrap).read_text()), args.cache, args.tags, args.output, args.workers, args.data_directory)
