"""Strict, dependency-free TACT bootstrap parsers and verified loose-object access.

Format references: CascLib/src/CascStructs.h and overtools/TACTLib.
Unsupported format versions/codecs fail rather than producing partial installs.
"""
import hashlib
from pathlib import Path, PurePosixPath
import re
import struct
import zlib

from ngdp import get, retry_request, sri

MAX_SIZE = 256 * 1024 * 1024


def md5(data):
    return hashlib.md5(data).hexdigest()


def key(value):
    if not isinstance(value, str) or not re.fullmatch('[0-9a-f]{32}', value):
        raise ValueError(f'invalid 16-byte TACT key: {value!r}')
    return value


class Reader:
    def __init__(self, data):
        self.data, self.offset = data, 0

    def read(self, size):
        if size < 0 or self.offset + size > len(self.data):
            raise ValueError(f'truncated manifest at byte {self.offset}')
        result = self.data[self.offset:self.offset + size]
        self.offset += size
        return result

    def uint(self, size):
        return int.from_bytes(self.read(size), 'big')

    def string(self):
        end = self.data.find(b'\0', self.offset)
        if end < 0:
            raise ValueError('unterminated manifest string')
        result = self.read(end - self.offset).decode('utf-8')
        self.read(1)
        return result

    def finish(self):
        if self.offset != len(self.data):
            raise ValueError(f'unexpected trailing manifest bytes: {len(self.data) - self.offset}')


def config(data):
    fields = {}
    for line in data.decode('utf-8').splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        name, sep, value = line.partition('=')
        if not sep or name.strip() in fields:
            raise ValueError('malformed or duplicate config field')
        fields[name.strip()] = value.split()
    return fields


def decode_frame(frame, limit):
    if not frame:
        raise ValueError('empty BLTE frame')
    if frame[:1] == b'N':
        result = frame[1:]
    elif frame[:1] == b'Z':
        decoder = zlib.decompressobj()
        try:
            result = decoder.decompress(frame[1:], limit + 1)
        except zlib.error as error:
            raise ValueError('invalid BLTE zlib stream') from error
        if len(result) > limit or decoder.unconsumed_tail:
            raise ValueError('BLTE decoded size exceeds limit')
        if not decoder.eof or decoder.unused_data:
            raise ValueError('truncated or trailing zlib stream')
    else:
        raise ValueError(f'unsupported BLTE codec {frame[:1]!r}; encrypted/LZ4/recursive frames are not skipped')
    if len(result) > limit:
        raise ValueError('BLTE decoded size exceeds limit')
    return result


def blte_frames(data, ekey, limit=MAX_SIZE):
    """Verify encoded identity, complete chunk table and every chunk checksum."""
    reader = Reader(data)
    if reader.read(4) != b'BLTE':
        raise ValueError('missing BLTE signature')
    header_size = reader.uint(4)
    if header_size == 0:
        if md5(data) != key(ekey):
            raise ValueError('BLTE EKey mismatch')
        return [(reader.read(len(data) - 8), None)]
    if header_size > len(data) or md5(data[:header_size]) != key(ekey):
        raise ValueError('BLTE header EKey mismatch')
    if reader.uint(1) != 15:
        raise ValueError('unsupported BLTE table format')
    count = reader.uint(3)
    if count == 0 or header_size != 12 + 24 * count:
        raise ValueError('invalid BLTE header size/count')
    table = [(reader.uint(4), reader.uint(4), reader.read(16).hex()) for _ in range(count)]
    if sum(encoded for encoded, _, _ in table) != len(data) - header_size:
        raise ValueError('BLTE encoded sizes mismatch')
    if sum(decoded for _, decoded, _ in table) > limit:
        raise ValueError('BLTE decoded size exceeds limit')
    frames = []
    for encoded, decoded, checksum in table:
        frame = reader.read(encoded)
        if md5(frame) != checksum:
            raise ValueError('BLTE chunk checksum mismatch')
        frames.append((frame, decoded))
    reader.finish()
    return frames


def blte(data, ekey, ckey=None, limit=MAX_SIZE):
    parts = []
    for frame, size in blte_frames(data, ekey, limit):
        part = decode_frame(frame, limit if size is None else size)
        if size is not None and len(part) != size:
            raise ValueError('BLTE chunk decoded size mismatch')
        parts.append(part)
    result = b''.join(parts)
    if ckey is not None and md5(result) != key(ckey):
        raise ValueError('decoded CKey mismatch')
    return result


def storage_payload(data, ekey, ckey, size):
    """Return verified plaintext, or None for an intact encrypted resource.

    Installations retain encrypted bytes for the official client's key provider.
    None explicitly means the decoded CKey was NOT verified. Ordinary install
    files and bootstrap manifests must still pass the strict plaintext decoder.
    """
    key(ckey)
    limit = max(MAX_SIZE, size)
    frames = blte_frames(data, ekey, limit)
    if not any(frame[:1] == b'E' for frame, _ in frames):
        decoded = blte(data, ekey, ckey, limit)
        if len(decoded) != size:
            raise ValueError('locked decoded size mismatch')
        return decoded
    if all(length is not None for _, length in frames) and sum(length for _, length in frames) != size:
        raise ValueError('encrypted BLTE declared size mismatch')
    for frame, length in frames:
        if frame[:1] == b'E':
            envelope = Reader(frame[1:])
            name_size = envelope.uint(1)
            if name_size not in (0, 8):
                raise ValueError('unsupported BLTE encryption key name size')
            envelope.read(name_size)
            iv_size = envelope.uint(1)
            if iv_size not in (4, 8):
                raise ValueError('unsupported BLTE encryption IV size')
            envelope.read(iv_size)
            if envelope.read(1) not in (b'S', b'A'):
                raise ValueError('unsupported BLTE encryption type')
            if envelope.offset == len(envelope.data):
                raise ValueError('empty BLTE ciphertext')
        else:
            decoded = decode_frame(frame, limit if length is None else length)
            if length is not None and len(decoded) != length:
                raise ValueError('BLTE chunk decoded size mismatch')
    return None


def tags(reader, count, entries):
    result = []
    for _ in range(count):
        result.append({'name': reader.string(), 'type': reader.uint(2),
                       'bitmap': reader.read((entries + 7) // 8)})
    if len({t['name'] for t in result}) != len(result):
        raise ValueError('duplicate manifest tag name')
    return result


def selected(manifest, names):
    """OR within normal tag types; alternate content also retains common files.

    0x4000 is the Alternate category (TACT.Net TagTypeHelper). Files with
    none of that category's flags are base content, not a different platform.
    Leaving a category unspecified preserves the existing catalog selection.
    """
    known = {t['name']: t for t in manifest['tags']}
    unknown = set(names) - known.keys()
    if unknown:
        raise ValueError('unknown tags: ' + ', '.join(sorted(unknown)))
    groups = {}
    for name in names:
        tag = known[name]
        groups.setdefault(tag['type'], []).append(tag['bitmap'])
    # An explicitly selected alternate cannot discard untagged base files.
    alternate = [tag['bitmap'] for tag in manifest['tags'] if tag['type'] == 0x4000]

    def matches(index, kind, group):
        byte, bit = index // 8, 128 >> (index % 8)
        return any(bitmap[byte] & bit for bitmap in group) or (
            kind == 0x4000 and not any(bitmap[byte] & bit for bitmap in alternate))

    return [entry for index, entry in enumerate(manifest['entries'])
            if all(matches(index, kind, group) for kind, group in groups.items())]


def install(data):
    r = Reader(data)
    if r.read(2) != b'IN' or r.uint(1) != 1 or r.uint(1) != 16:
        raise ValueError('only install manifest v1 with 16-byte CKeys is supported')
    tag_count, count = r.uint(2), r.uint(4)
    manifest_tags = tags(r, tag_count, count)
    entries = []
    for _ in range(count):
        name, ckey, size = r.string(), r.read(16).hex(), r.uint(4)
        name = safe_path(name)
        entries.append({'path': name, 'ckey': ckey, 'size': size})
    r.finish()
    return {'version': 1, 'tags': manifest_tags, 'entries': entries}


def safe_path(name):
    name = name.replace('\\', '/')
    path = PurePosixPath(name)
    if not name or name.startswith('/') or ':' in name or '\x00' in name or any(
            part in ('', '.', '..') for part in name.split('/')):
        raise ValueError(f'unsafe installation path: {name!r}')
    return str(path)


def download(data):
    r = Reader(data)
    if r.read(2) != b'DL':
        raise ValueError('missing download manifest signature')
    version, key_size, checksum = r.uint(1), r.uint(1), r.uint(1)
    if version not in (1, 2, 3) or key_size != 16 or checksum not in (0, 1):
        raise ValueError('unsupported download manifest header')
    count, tag_count = r.uint(4), r.uint(2)
    flag_size = r.uint(1) if version >= 2 else 0
    base_priority = 0
    if version >= 3:
        base_priority = struct.unpack('b', r.read(1))[0]
        if r.read(3) != b'\0' * 3:
            raise ValueError('unsupported download reserved bytes')
    entries = []
    for _ in range(count):
        entry = {'ekey': r.read(16).hex(), 'size': r.uint(5), 'priority': r.uint(1) + base_priority}
        if checksum:
            entry['checksum'] = r.read(4).hex()
        if flag_size:
            entry['flags'] = r.read(flag_size).hex()
        entries.append(entry)
    manifest_tags = tags(r, tag_count, count)
    r.finish()
    return {'version': version, 'tags': manifest_tags, 'entries': entries}


def encoding(data, wanted=None):
    """Validate CKey pages; retain requested lookups rather than millions of keys."""
    r = Reader(data)
    if r.read(2) != b'EN' or r.uint(1) != 1 or r.uint(1) != 16 or r.uint(1) != 16:
        raise ValueError('unsupported encoding header')
    cpage_size, epage_size = r.uint(2) * 1024, r.uint(2) * 1024
    ccount, ecount = r.uint(4), r.uint(4)
    if r.uint(1) != 0 or not cpage_size or not epage_size:
        raise ValueError('unsupported encoding page format')
    r.read(r.uint(4))  # Encoding spec strings, not needed to decode fetched BLTE.
    headers = [(r.read(16).hex(), r.read(16).hex()) for _ in range(ccount)]
    results = {}
    for first, checksum in headers:
        page = r.read(cpage_size)
        if md5(page) != checksum:
            raise ValueError('encoding CKey page checksum mismatch')
        p = Reader(page)
        first_seen = None
        while p.offset < len(page):
            count = p.uint(1)
            if count == 0:
                if any(page[p.offset:]):
                    raise ValueError('nonzero encoding page padding')
                break
            size, ckey = p.uint(5), p.read(16).hex()
            ekeys = [p.read(16).hex() for _ in range(count)]
            first_seen = first_seen or ckey
            if wanted is None or ckey in wanted:
                if ckey in results:
                    raise ValueError('duplicate encoding CKey')
                results[ckey] = {'size': size, 'ekeys': ekeys}
        if first_seen != first:
            raise ValueError('encoding page first-key mismatch')
    # EKey pages and trailing encoding specifications are not interpreted, but
    # validate all pages referenced by the header. The whole CKey was checked by
    # blte() before this parser is called.
    eheaders = [(r.read(16).hex(), r.read(16).hex()) for _ in range(ecount)]
    for first, checksum in eheaders:
        page = r.read(epage_size)
        if md5(page) != checksum or page[:16].hex() != first:
            raise ValueError('encoding EKey page checksum/first-key mismatch')
    return results


def read_config(descriptor):
    data = get(descriptor['url'])
    if md5(data) != key(descriptor['key']) or sri(data) != descriptor['hash']:
        raise ValueError('locked config hash mismatch')
    return data


def fetch_object(release, ekey, ckey, cache, max_size=MAX_SIZE):
    """Fetch a loose CDN object; archive-only objects fail explicitly for now."""
    key(ekey)
    path = Path(cache) / ekey
    if path.exists():
        data = path.read_bytes()
        if len(data) > max_size:
            raise ValueError('cached object exceeds size limit')
        decoded = blte(data, ekey, ckey, max_size)
        return data, decoded, None
    errors = []
    for server in release['cdnServers']:
        url = f'{server.rstrip("/")}/{release["cdnPath"]}/data/{ekey[:2]}/{ekey[2:4]}/{ekey}'
        try:
            data = get(url, max_size)
            decoded = blte(data, ekey, ckey, max_size)
            path.parent.mkdir(parents=True, exist_ok=True)
            # Atomic publication prevents interrupted fetches becoming cache hits.
            import os
            import tempfile
            fd, tmp = tempfile.mkstemp(dir=path.parent, prefix='.' + ekey)
            try:
                with os.fdopen(fd, 'wb') as stream:
                    stream.write(data)
                os.replace(tmp, path)
            finally:
                if os.path.exists(tmp):
                    os.unlink(tmp)
            return data, decoded, url
        except (OSError, ValueError) as error:
            errors.append(f'{url}: {error}')
    raise ValueError('loose object unavailable (archive range backend not implemented): ' + '; '.join(errors))


def prefetch(release, cache):
    build_data, cdn_data = read_config(release['buildConfig']), read_config(release['cdnConfig'])
    build = config(build_data)
    result = {'schemaVersion': 1, 'release': release, 'objects': {}}
    for name in ('encoding', 'install', 'download'):
        pair = build.get(name, [])
        if len(pair) != 2:
            raise ValueError(f'{name}: build config must provide CKey and EKey')
        ckey, ekey = map(key, pair)
        raw, decoded, url = fetch_object(release, ekey, ckey, cache)
        sizes = build.get(name + '-size', [])
        if sizes and (len(sizes) != 2 or list(map(int, sizes)) != [len(decoded), len(raw)]):
            raise ValueError(f'{name}: build config sizes mismatch')
        # Cache hits still need a stable source URL in the generated lock.
        url = url or f'{release["cdnServers"][0].rstrip("/")}/{release["cdnPath"]}/data/{ekey[:2]}/{ekey[2:4]}/{ekey}'
        result['objects'][name] = {'ckey': ckey, 'ekey': ekey, 'url': url,
                                   'hash': sri(raw), 'encodedSize': len(raw), 'decodedSize': len(decoded)}
    return result


def load_manifests(lock, cache):
    results = {}
    for name, descriptor in lock['objects'].items():
        raw, decoded, _ = fetch_object(lock['release'], descriptor['ekey'], descriptor['ckey'], cache)
        if sri(raw) != descriptor['hash'] or len(raw) != descriptor['encodedSize'] or len(decoded) != descriptor['decodedSize']:
            raise ValueError(f'{name}: bootstrap lock hash/size mismatch')
        results[name] = decoded
    return results


def unique_install(entries):
    by_path = {}
    for entry in entries:
        folded = entry['path'].casefold()
        if folded in by_path:
            previous = by_path[folded]
            if (previous['ckey'], previous['size']) != (entry['ckey'], entry['size']):
                raise ValueError('conflicting selected installation path: ' + entry['path'])
        else:
            by_path[folded] = entry
    return list(by_path.values())


def macho_architectures(data):
    """Read CPU identities without modifying signed Mach-O bytes."""
    magic = data[:4]
    cpus = []
    if magic in (b'\xcf\xfa\xed\xfe', b'\xfe\xed\xfa\xcf'):
        if len(data) < 32:
            raise ValueError('truncated Mach-O header')
        cpus = [int.from_bytes(data[4:8], 'little' if magic[0] == 0xcf else 'big')]
    elif magic in (b'\xca\xfe\xba\xbe', b'\xbe\xba\xfe\xca', b'\xca\xfe\xba\xbf', b'\xbf\xba\xfe\xca'):
        endian = 'big' if magic[0] == 0xca else 'little'
        count = int.from_bytes(data[4:8], endian)
        width = 32 if magic in (b'\xca\xfe\xba\xbf', b'\xbf\xba\xfe\xca') else 20
        if not 1 <= count <= 16 or len(data) < 8 + count * width:
            raise ValueError('invalid universal Mach-O header')
        cpus = [int.from_bytes(data[8 + i * width:12 + i * width], endian) for i in range(count)]
    else:
        raise ValueError('macOS application executable is not a 64-bit Mach-O')
    names = {0x01000007: 'x86_64', 0x0100000c: 'arm64'}
    if not any(cpu in names for cpu in cpus):
        raise ValueError('macOS application has no supported CPU architecture')
    return sorted({names[cpu] for cpu in cpus if cpu in names})


def macos_launch(lock, cache, names, application, pattern=''):
    import plistlib
    plan = make_plan(lock, cache, names)
    apps = [e['path'][:-len('/Contents/Info.plist')] for e in plan['installFiles']
            if e['path'].endswith('.app/Contents/Info.plist')]
    matches = [app for app in apps if re.fullmatch(pattern, app)] if pattern else [
        app for app in apps if app.casefold() == safe_path(application).casefold()]
    if len(matches) != 1:
        raise ValueError('native application is absent or ambiguous in the selected install manifest')
    app = matches[0]
    descriptor = prefetch_file(lock, cache, app + '/Contents/Info.plist', names)
    raw = (Path(cache) / descriptor['ekey']).read_bytes()
    plist = plistlib.loads(blte(raw, descriptor['ekey'], descriptor['ckey']))
    binary = plist.get('CFBundleExecutable')
    if not isinstance(binary, str) or not binary or '/' in binary or '\\' in binary or binary in ('.', '..'):
        raise ValueError('invalid CFBundleExecutable')
    descriptor = prefetch_file(lock, cache, app + '/Contents/MacOS/' + binary, names)
    raw = (Path(cache) / descriptor['ekey']).read_bytes()
    architectures = macho_architectures(blte(raw, descriptor['ekey'], descriptor['ckey']))
    return {'application': app, 'executable': descriptor['path'], 'architectures': architectures}


def make_plan(lock, cache, names):
    data = load_manifests(lock, cache)
    ins, dl = install(data['install']), download(data['download'])
    loose, objects = unique_install(selected(ins, names)), selected(dl, names)
    lookup = encoding(data['encoding'], {entry['ckey'] for entry in loose})
    for entry in loose:
        mapping = lookup.get(entry['ckey'])
        if mapping is None or mapping['size'] != entry['size']:
            raise ValueError('install entry missing or mismatched in encoding table: ' + entry['path'])
        entry['ekeys'] = mapping['ekeys']
    return {'schemaVersion': 1, 'product': lock['release']['product'],
            'buildConfig': lock['release']['buildConfig']['key'], 'selectedTags': names,
            'installFiles': loose, 'downloadObjects': objects,
            'totalEncodedBytes': sum(entry['size'] for entry in objects),
            'availableTags': {
                'install': [{'name': t['name'], 'type': t['type']} for t in ins['tags']],
                'download': [{'name': t['name'], 'type': t['type']} for t in dl['tags']]}}


def inspect(lock, cache):
    data = load_manifests(lock, cache)
    encoding(data['encoding'], set())
    result = {'product': lock['release']['product'], 'version': lock['release']['version']}
    for name, parser in [('install', install), ('download', download)]:
        parsed = parser(data[name])
        result[name] = {'version': parsed['version'], 'entryCount': len(parsed['entries']),
                        'tags': [{'name': t['name'], 'type': t['type']} for t in parsed['tags']]}
    return result


def verify_manifest_directory(directory, lock):
    data = {}
    for name in ('encoding', 'install', 'download'):
        descriptor = lock['objects'][name]
        raw = (Path(directory) / (name + '.blte')).read_bytes()
        if sri(raw) != descriptor['hash'] or len(raw) != descriptor['encodedSize']:
            raise ValueError(f'{name}: locked encoded hash/size mismatch')
        decoded = blte(raw, descriptor['ekey'], descriptor['ckey'])
        if len(decoded) != descriptor['decodedSize']:
            raise ValueError(f'{name}: locked decoded size mismatch')
        data[name] = decoded
        (Path(directory) / (name + '.decoded')).write_bytes(decoded)
    install(data['install'])
    download(data['download'])
    encoding(data['encoding'], set())


def archive_index(data, archive):
    """Verify footer→TOC→page hashes, then parse archive EKey locations."""
    footer = None
    for toc_width, checksum_width in ((8, 8), (16, 16), (16, 8)):
        length = toc_width + 12 + checksum_width
        if len(data) < length:
            continue
        candidate = data[-length:]
        params = candidate[toc_width:toc_width + 8]
        if params[:3] == b'\x01\0\0' and params[7] == checksum_width:
            if md5(candidate) == key(archive):
                footer = candidate
                break
    if footer is None:
        raise ValueError('archive index footer identity/format mismatch')
    fields = footer[toc_width:toc_width + 12]
    if hashlib.md5(fields + bytes(checksum_width)).digest()[:checksum_width] != footer[-checksum_width:]:
        raise ValueError('archive index footer checksum mismatch')
    page_size, offset_size, size_width, key_width = fields[3] * 1024, fields[4], fields[5], fields[6]
    if not page_size or offset_size not in (0, 4) or size_width != 4 or key_width != 16:
        raise ValueError('unsupported archive index field widths')
    count = int.from_bytes(fields[8:12], 'little')
    entry_size = key_width + size_width + offset_size
    capacity = page_size // entry_size
    pages = (count + capacity - 1) // capacity
    if len(data) != pages * (page_size + key_width + checksum_width) + len(footer):
        raise ValueError('archive index size/count mismatch')
    toc = data[pages * page_size:-len(footer)]
    if hashlib.md5(toc).digest()[:toc_width] != footer[:toc_width]:
        raise ValueError('archive index TOC checksum mismatch')
    results = {}
    previous = None
    for page_index in range(pages):
        page = data[page_index * page_size:(page_index + 1) * page_size]
        page_checksum = toc[pages * key_width + page_index * checksum_width:pages * key_width + (page_index + 1) * checksum_width]
        if hashlib.md5(page).digest()[:checksum_width] != page_checksum:
            raise ValueError('archive index page checksum mismatch')
        entries = min(capacity, count - page_index * capacity)
        p = Reader(page)
        for _ in range(entries):
            ekey, size, offset = p.read(key_width).hex(), p.uint(size_width), p.uint(offset_size)
            if ekey == '0' * 32 or size == 0 or (previous is not None and ekey <= previous):
                raise ValueError('invalid/unsorted archive index entry')
            previous = ekey
            results[ekey] = ({'archive': archive, 'offset': offset, 'size': size} if offset_size else {'kind': 'loose', 'size': size})
        if any(page[p.offset:]):
            raise ValueError('nonzero archive index padding')
        if bytes.fromhex(previous) != toc[page_index * key_width:(page_index + 1) * key_width]:
            raise ValueError('archive index TOC last-key mismatch')
    return results


def bounded_get(url, limit, offset=None, size=None):
    return retry_request(lambda: _bounded_get(url, limit, offset, size))


def _bounded_get(url, limit, offset=None, size=None):
    import urllib.request
    headers = {}
    if offset is not None:
        headers['Range'] = f'bytes={offset}-{offset + size - 1}'
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=30) as response:
        if offset is not None:
            value = response.headers.get('Content-Range', '')
            match = re.fullmatch(r'bytes (\d+)-(\d+)/(\d+)', value)
            if response.status != 206 or match is None or list(map(int, match.groups()[:2])) != [offset, offset + size - 1] or int(match.group(3)) <= offset + size - 1:
                raise ValueError('server did not honor exact archive byte range')
        length = response.headers.get('Content-Length')
        if length and int(length) > limit:
            raise ValueError('response exceeds size limit')
        data = response.read(limit + 1)
    if len(data) > limit:
        raise ValueError('response exceeds size limit')
    if size is not None and len(data) < size:
        raise OSError(f'truncated CDN response: expected {size} bytes, received {len(data)}')
    if size is not None and len(data) != size:
        raise ValueError('response length mismatch')
    return data


def atomic_bytes(path, data):
    import os
    import tempfile
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix='.' + path.name)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def prefetch_indexes(lock, cache):
    from concurrent.futures import ThreadPoolExecutor
    release = lock['release']
    cdn = config(read_config(release['cdnConfig']))
    archives = cdn.get('archives', []) + cdn.get('file-index', [])
    if not archives:
        raise ValueError('CDN config has no archive indexes')

    def fetch(archive):
        key(archive)
        cached = Path(cache) / (archive + '.index')
        if cached.exists():
            data = cached.read_bytes()
            archive_index(data, archive)
            return {'archive': archive, 'hash': sri(data), 'size': len(data),
                    'url': f'{release["cdnServers"][0].rstrip("/")}/{release["cdnPath"]}/data/{archive[:2]}/{archive[2:4]}/{archive}.index'}
        errors = []
        for server in release['cdnServers']:
            url = f'{server.rstrip("/")}/{release["cdnPath"]}/data/{archive[:2]}/{archive[2:4]}/{archive}.index'
            try:
                data = bounded_get(url, 16 * 1024 * 1024)
                archive_index(data, archive)
                atomic_bytes(cached, data)
                return {'archive': archive, 'hash': sri(data), 'size': len(data), 'url': url}
            except (OSError, ValueError) as error:
                errors.append(str(error))
        raise ValueError(f'{archive}: index unavailable: ' + '; '.join(errors))

    with ThreadPoolExecutor(max_workers=6) as executor:
        descriptors = list(executor.map(fetch, archives))
    return dict(lock, archives=descriptors)


def locate_archive(lock, cache, candidates):
    for descriptor in lock.get('archives', []):
        path = Path(cache) / (descriptor['archive'] + '.index')
        data = path.read_bytes() if path.exists() else bounded_get(descriptor['url'], 16 * 1024 * 1024)
        if sri(data) != descriptor['hash'] or len(data) != descriptor['size']:
            raise ValueError('locked archive index hash/size mismatch')
        locations = archive_index(data, descriptor['archive'])
        for ekey in candidates:
            if ekey in locations:
                return ekey, locations[ekey]
    raise ValueError('no encoding candidate found in locked archive indexes')


def prefetch_file(lock, cache, path, names):
    plan = make_plan(lock, cache, names)
    matching = [e for e in plan['installFiles'] if e['path'] == safe_path(path)]
    if len(matching) != 1:
        raise ValueError('path is not a selected install-manifest entry')
    entry = matching[0]
    ekey, location = locate_archive(lock, cache, entry['ekeys'])
    errors = []
    release = lock['release']
    archive = location.get('archive')
    if location['size'] > MAX_SIZE:
        raise ValueError('file exceeds supported size limit')
    for server in release['cdnServers']:
        object_key = archive or ekey
        url = f'{server.rstrip("/")}/{release["cdnPath"]}/data/{object_key[:2]}/{object_key[2:4]}/{object_key}'
        try:
            raw = bounded_get(url, location['size'], location.get('offset'), location['size'])
            decoded = blte(raw, ekey, entry['ckey'])
            if len(decoded) != entry['size']:
                raise ValueError('install file decoded size mismatch')
            atomic_bytes(Path(cache) / ekey, raw)
            result = {'schemaVersion': 1, 'release': release, 'path': entry['path'],
                      'tags': names, 'ckey': entry['ckey'], 'ekey': ekey,
                      'hash': sri(raw), 'decodedHash': sri(decoded),
                      'decodedSize': len(decoded), 'encodedSize': len(raw),
                      'source': dict(location, url=url, kind='archive' if archive else 'loose')}
            return result
        except (OSError, ValueError) as error:
            errors.append(f'{url}: {error}')
    raise ValueError('archive object unavailable: ' + '; '.join(errors))


def materialize_file(descriptor, output):
    location = descriptor.get('source', descriptor.get('archive'))
    raw = bounded_get(location['url'], descriptor['encodedSize'], location.get('offset'), descriptor['encodedSize'])
    if sri(raw) != descriptor['hash']:
        raise ValueError('locked file encoded SHA256 mismatch')
    decoded = blte(raw, descriptor['ekey'], descriptor['ckey'])
    if sri(decoded) != descriptor['decodedHash'] or len(decoded) != descriptor['decodedSize']:
        raise ValueError('locked file decoded hash/size mismatch')
    atomic_bytes(output, decoded)


def resolve_plan(lock, cache, names):
    plan = make_plan(lock, cache, names)
    locations = {}
    for descriptor in lock.get('archives', []):
        path = Path(cache) / (descriptor['archive'] + '.index')
        data = path.read_bytes() if path.exists() else bounded_get(descriptor['url'], 16 * 1024 * 1024)
        if sri(data) != descriptor['hash'] or len(data) != descriptor['size']:
            raise ValueError('locked archive index hash/size mismatch')
        for ekey, location in archive_index(data, descriptor['archive']).items():
            locations.setdefault(ekey, location)
    if not locations:
        raise ValueError('run prefetch-indexes before resolving the download plan')
    for entry in plan['downloadObjects']:
        location = locations.get(entry['ekey'])
        if location is None or location['size'] != entry['size']:
            raise ValueError('download object missing/size mismatch in locked indexes: ' + entry['ekey'])
        entry['location'] = location
    for entry in plan['installFiles']:
        candidate = next((k for k in entry['ekeys'] if k in locations), None)
        if candidate is None:
            raise ValueError('install file missing from locked indexes: ' + entry['path'])
        entry['ekey'] = candidate
        entry['location'] = locations[candidate]
    plan['release'] = lock['release']
    plan['bootstrapHash'] = sri(__import__('json').dumps(lock, sort_keys=True, separators=(',', ':')).encode())
    return plan
