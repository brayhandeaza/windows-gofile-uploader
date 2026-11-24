#!/usr/bin/env python3
"""Save verified Spanish 1080p sources from Webtor Discover's Stremio providers."""

import argparse
import base64
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import ssl
import tempfile
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, urlencode, urlparse
from urllib.request import Request, urlopen

import certifi

from queue_pelipanda_streamtape import ROOT, PROJECT, APIError, exclusive


OUTPUT = ROOT / 'output/WebTorrent.movie.json'
MAX_MOVIE_BYTES = 5_000_000_000
TARGET_MOVIE_BYTES = 4_000_000_000


CATALOG = 'https://v3-cinemeta.strem.io'
ADDONS = ('https://mediafusion.forthewizards.uk', 'https://torrentio.stremio.ru')
BAD_RELEASE = re.compile(r'1\s*x\s*bet|x[\W_]*bet|xbet|\b(?:hdcam|hdts|hdtc|telesync|telecine|camrip|cam|hdtc)\b|(?:^|[. _-])(?:ts|tc)(?:[. _-]|$)', re.I)
SPANISH = re.compile(r'\b(?:spanish|espa[nñ]ol|castellano|latino|latina|latam|spa|esp|es-419)\b|🇪🇸|🇲🇽', re.I)


def allowed_size(value, maximum=MAX_MOVIE_BYTES):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and 0 < value <= maximum


def now():
    return datetime.now(timezone.utc).isoformat()


def load_key():
    path = PROJECT / '.env.local'
    if path.exists():
        for line in path.read_text().splitlines():
            key, sep, value = line.strip().removeprefix('export ').partition('=')
            if sep and key.isidentifier():
                os.environ.setdefault(key, value.strip().strip('"\''))
    key = os.environ.get('WEBTOR_API_KEY')
    if not key:
        raise ValueError('Falta WEBTOR_API_KEY en .env.local')
    return key


def expires_at(url):
    """Expiry is a scheduling hint, not JWT authentication or verification."""
    query = parse_qs(urlparse(url).query)
    try:
        token = query.get('token', [''])[0].split('.')[1]
        payload = json.loads(base64.urlsafe_b64decode(token + '=' * (-len(token) % 4)))
        return datetime.fromtimestamp(float(payload['exp']), timezone.utc).isoformat()
    except (ValueError, TypeError, KeyError, IndexError, OverflowError):
        return None


def save(path, document):
    path.parent.mkdir(parents=True, exist_ok=True)
    document['updated_at'] = now()
    document['total'] = len(document['movies'])
    document['total_sources'] = sum(len(movie['sources']) for movie in document['movies'])
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                     prefix=path.name + '.', delete=False) as file:
        temporary = Path(file.name)
        json.dump(document, file, ensure_ascii=False, indent=2)
        file.write('\n')
    temporary.chmod(0o600)
    temporary.replace(path)


def read_catalog(path):
    if not path.exists():
        return {'schema_version': 1, 'source': 'webtor_discover', 'media_type': 'movie',
                'movies': [], 'errors': {}, 'rejections': {}}
    document = json.loads(path.read_text())
    if document.get('schema_version') != 1 or document.get('source') != 'webtor_discover':
        raise ValueError('El JSON existente no es un catálogo de Webtor Discover')
    return document


def stream_text(stream):
    return ' '.join(str(value or '') for value in (
        stream.get('name'), stream.get('description'), stream.get('title'),
        stream.get('behaviorHints', {}).get('filename')))


def candidate(stream, allow_4k=False):
    text = stream_text(stream)
    if BAD_RELEASE.search(text):
        return None
    quality = r'\b(?:1080p?|2160p?|4k|uhd)\b' if allow_4k else r'\b1080p?\b'
    if not re.search(quality, text, re.I) or not SPANISH.search(text):
        return None
    advertised = stream.get('behaviorHints', {}).get('videoSize')
    if isinstance(advertised, (int, float)) and not allowed_size(advertised, float('inf')):
        return None
    key = str(stream.get('infoHash') or '').lower()
    if not re.fullmatch(r'[a-f0-9]{40}', key):
        return None
    trackers = [source[8:] for source in stream.get('sources', []) if source.startswith('tracker:')]
    magnet = 'magnet:?' + urlencode([('xt', 'urn:btih:' + key), *[('tr', tracker) for tracker in trackers]])
    return {'resource_id': key, 'magnet': magnet, 'file_index': stream.get('fileIdx'),
            'advertised_filename': stream.get('behaviorHints', {}).get('filename'),
            'provider': stream.get('_provider', 'unknown')}


def audio_metadata(probe):
    audio = [stream for stream in probe.get('streams', []) if stream.get('codec_type') == 'audio']
    result = []
    defaults = [stream for stream in audio if stream.get('disposition', {}).get('default') == 1]
    if not defaults:
        defaults = audio[:1]
    for stream in audio:
        tags = stream.get('tags', {})
        language = str(tags.get('language') or '').lower()
        spanish = language in ('spa', 'esp', 'es') or language.startswith(('es-', 'es_'))
        # Titles can fill in an absent language tag, but never override a different tag.
        if not language or language == 'und':
            spanish = bool(SPANISH.search(tags.get('title', '')))
        result.append({'language': language or 'und', 'title': tags.get('title'),
                       'spanish': spanish, 'default': stream in defaults,
                       'index': stream.get('index')})
    return result


def source_rank(source):
    return (source.get('size_bytes') if allowed_size(source.get('size_bytes'), float('inf')) else float('inf'),
            not source.get('spanish_default'),
            not source.get('latin_spanish'), source.get('cached_complete') is not True,
            -(source.get('peers') or 0))


class Webtor:
    def __init__(self, key, timeout=30, max_source_bytes=float('inf')):
        self.key = key
        self.timeout = timeout
        self.max_source_bytes = max_source_bytes
        self.context = ssl.create_default_context(cafile=certifi.where())

    def json(self, url, body=None, headers=None, timeout=None):
        try:
            with urlopen(Request(url, data=body, headers={'User-Agent': 'Mozilla/5.0', **(headers or {})}),
                         timeout=timeout or self.timeout, context=self.context) as response:
                return json.load(response)
        except HTTPError as error:
            raise APIError(f'HTTP {error.code} en {urlparse(url).hostname}') from None
        except (URLError, OSError, ValueError):
            raise APIError(f'Respuesta ausente o inválida de {urlparse(url).hostname}') from None

    def api(self, route, body=None):
        return self.json('https://api.webtor.io/v1/' + route, body,
                         {'Authorization': 'Bearer ' + self.key,
                          'Content-Type': 'application/octet-stream'})

    def streams(self, addon, imdb):
        response = self.json(addon.rstrip('/') + '/stream/movie/' + quote(imdb, safe='') + '.json')
        return [dict(stream, _provider=urlparse(addon).hostname) for stream in response.get('streams', [])]

    def export(self, resource_id, file_id, types='download,media_probe,torrent_client_stat'):
        return self.api(f'resource/{resource_id}/export/{file_id}?types={types}')

    def cache_stat(self, exports):
        download = exports.get('download', {})
        if download.get('meta', {}).get('cached'):
            return {'cached_complete': True, 'peers': None}
        link = exports.get('torrent_client_stat', {}).get('url')
        if not link:
            return {'cached_complete': None, 'peers': None}
        # This endpoint is SSE, not a finite JSON document. Read only its first update.
        try:
            with urlopen(link, timeout=10, context=self.context) as response:
                for _ in range(20):
                    line = response.readline(2 * 1024 ** 2)
                    if not line:
                        break
                    if line.startswith(b'data:'):
                        data = json.loads(line[5:])
                        total, completed = data.get('total', 0), data.get('completed', 0)
                        return {'cached_complete': bool(total and completed >= total),
                                'cached_bytes': completed, 'peers': data.get('peers')}
        except (OSError, ValueError):
            pass
        return {'cached_complete': None, 'peers': None}

    def resolve(self, source):
        resource = self.api('resource/', source['magnet'].encode())
        if resource['id'] != source['resource_id']:
            raise APIError('Webtor devolvió otro torrent')
        file = resource.get('file')
        if not file:
            listing = self.api('resource/' + resource['id'] + '/list?limit=1000&output=list')
            if listing.get('items_count', 0) > len(listing.get('items', [])):
                raise APIError('Listado de archivos incompleto; no se seleccionará un video ambiguo')
            files = [item for item in listing.get('items', [])
                     if item.get('type') == 'file' and item.get('media_format') == 'video'
                     and not re.search(r'\b(sample|trailer)\b', item.get('name', ''), re.I)]
            if source.get('file_index') is not None:
                files = [item for item in files if str(item.get('index')) == str(source['file_index'])]
            if len(files) != 1:
                raise APIError('Torrent con varios videos: selección no inequívoca')
            file = files[0]
        if BAD_RELEASE.search(file.get('name', '')):
            raise APIError('Archivo XBet/CAM/TS excluido')
        if file.get('media_format') != 'video':
            raise APIError('El archivo seleccionado no es un video')
        if not allowed_size(file.get('size'), self.max_source_bytes):
            raise APIError('Video sin tamaño verificado o fuera del límite configurado; excluido')
        exported = self.export(resource['id'], file['id'])
        exports = exported.get('exports', {})
        url = exports.get('download', {}).get('url')
        probe_url = exports.get('media_probe', {}).get('url')
        if not url or urlparse(url).scheme != 'https' or not probe_url:
            raise APIError('No hay descarga o información de audio disponible')
        probe = self.json(probe_url)
        videos = [stream for stream in probe.get('streams', []) if stream.get('codec_type') == 'video']
        if not any(1800 <= stream.get('width', 0) <= 1920 and 0 < stream.get('height', 0) <= 1080 for stream in videos):
            raise APIError('El video no corresponde a 1080p')
        audio = audio_metadata(probe)
        spanish = [track for track in audio if track['spanish']]
        if not spanish:
            raise APIError('No hay audio español verificado (los subtítulos no cuentan)')
        return {**source, 'file_id': file['id'], 'filename': file['name'], 'size_bytes': file['size'],
                'resolution': '1080p', 'audio': audio, 'spanish_default': any(track['default'] for track in spanish),
                'latin_spanish': any(re.search(r'latin|latino|latam|es[-_]419', str(track), re.I) for track in spanish),
                'download_url': url, 'url_created_at': now(), 'url_expires_at': expires_at(url),
                **self.cache_stat(exports)}

    def refresh(self, source):
        try:
            result = self.export(source['resource_id'], source['file_id'], 'download')
        except APIError:
            return self.resolve(source)
        url = result.get('exports', {}).get('download', {}).get('url')
        if not url or urlparse(url).scheme != 'https':
            raise APIError('No se pudo renovar la descarga')
        return {**source, 'download_url': url, 'url_created_at': now(), 'url_expires_at': expires_at(url)}


def collect_movie(client, meta, addons, workers, on_source=None):
    streams, errors = [], []
    with ThreadPoolExecutor(max_workers=min(workers, len(addons))) as pool:
        futures = {pool.submit(client.streams, addon, meta['id']): addon for addon in addons}
        for future in as_completed(futures):
            try:
                streams.extend(future.result())
            except APIError as error:
                errors.append(str(error))
    choices = {}
    for stream in streams:
        source = candidate(stream)
        if source:
            choices.setdefault((source['resource_id'], source['file_index']), source)
    sources = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(client.resolve, source): source for source in choices.values()}
        for future in as_completed(futures):
            try:
                source = future.result()
                sources.append(source)
                if on_source and on_source(source):
                    for pending in futures:
                        pending.cancel()
                    break
            except APIError as error:
                errors.append(str(error))
    sources.sort(key=source_rank)
    tmdb = meta.get('moviedb_id')
    if not str(tmdb or '').isdigit() or int(tmdb) <= 0:
        raise APIError('La ficha no proporciona un TMDB válido')
    return {'imdb_id': meta['id'], 'tmdb_id': int(tmdb), 'title': meta['name'],
            'year': str(meta.get('year') or meta.get('releaseInfo') or '')[:4],
            'discover_url': 'https://webtor.io/discover?' + urlencode({'type': 'movie', 'id': meta['id']}),
            'sources': sources, 'source_errors': errors, 'checked_at': now()}, errors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--limit', type=int, default=10, help='Máximo de películas nuevas con fuentes verificadas')
    parser.add_argument('--scan-limit', type=int, default=100)
    parser.add_argument('--workers', type=int, default=2)
    parser.add_argument('--timeout', type=int, default=30)
    parser.add_argument('--imdb', help='Comprobar una película concreta, por ejemplo tt27729779')
    parser.add_argument('--addon-url', action='append', help='Base o manifest.json de los addons; repetir para varios')
    parser.add_argument('--output', type=Path, default=OUTPUT)
    parser.add_argument('--refresh', action='store_true')
    args = parser.parse_args()
    if min(args.limit, args.scan_limit, args.workers, args.timeout) < 1:
        parser.error('Los límites deben ser positivos')
    if args.imdb and not re.fullmatch(r'tt\d+', args.imdb):
        parser.error('--imdb requiere un ID tt12345')
    client = Webtor(load_key(), args.timeout)
    client.api('profile')  # Fail before scanning if the API key/plan is invalid.
    addons = list(dict.fromkeys(url.removesuffix('/manifest.json').rstrip('/') for url in (args.addon_url or ADDONS)))
    if any(urlparse(url).scheme != 'https' for url in addons):
        parser.error('Los addons deben usar HTTPS')
    with exclusive(args.output.with_suffix('.lock')):
        document = read_catalog(args.output)
        known = {movie['imdb_id']: movie for movie in document['movies']}
        scanned, added, offset, seen = 0, 0, 0, set()
        while scanned < args.scan_limit and added < args.limit:
            if args.imdb:
                metas = [client.json(CATALOG + '/meta/movie/' + args.imdb + '.json')['meta']]
            else:
                route = '/catalog/movie/top' + (f'/skip={offset}' if offset else '') + '.json'
                metas = client.json(CATALOG + route).get('metas', [])
            if not metas:
                break
            fresh = 0
            for meta in metas:
                imdb = meta['id']
                if imdb in seen:
                    continue
                seen.add(imdb)
                fresh += 1
                if imdb in known and not args.refresh:
                    continue
                scanned += 1
                print(f'Checking {meta["name"]} · {imdb}', flush=True)
                try:
                    movie, errors = collect_movie(client, meta, addons, args.workers)
                    if movie['sources']:
                        known[imdb] = movie
                        added += 1
                        document['errors'].pop(imdb, None)
                        document['rejections'].pop(imdb, None)
                        print(f'Saved: {len(movie["sources"])} Spanish 1080p download(s)', flush=True)
                    else:
                        document['rejections'][imdb] = {'title': meta['name'], 'checked_at': now(),
                            'reason': 'No verified Spanish 1080p sources', 'details': errors}
                        print('No eligible source; continuing.', flush=True)
                except APIError as error:
                    document['errors'][imdb] = str(error)
                    print(f'{imdb}: {error}', flush=True)
                document['movies'] = list(known.values())
                save(args.output, document)
                if scanned >= args.scan_limit or added >= args.limit:
                    break
            if args.imdb or not fresh:
                break
            offset += len(metas)
        print(f'Saved {added} movie(s) this run; checked {scanned}. JSON: {args.output}', flush=True)


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print('\nStopped. Saved movies remain available for the next run.', flush=True)
        raise SystemExit(130)
    except (APIError, ValueError, OSError) as error:
        print(f'Error: {error}', flush=True)
        raise SystemExit(1)
