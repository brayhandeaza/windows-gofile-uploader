#!/usr/bin/env python3
"""Queue a bounded movie batch through Webtor to Streamtape, then exit."""

import argparse
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
from portable_file_lock import exclusive as portable_exclusive
import json
import os
from pathlib import Path
import re
import sqlite3
import ssl
import unicodedata
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import Request, urlopen

import certifi


ROOT = Path(__file__).resolve().parents[1]
PROJECT = ROOT.parents[1]
TMDB = re.compile(r'\bTMDB\s*[:=_-]\s*(\d+)\b', re.I)


def credentials(require_webtor=True):
    path = PROJECT / '.env.local'
    if path.exists():
        for line in path.read_text().splitlines():
            key, sep, value = line.strip().removeprefix('export ').partition('=')
            if sep and key.isidentifier():
                os.environ.setdefault(key, value.strip().strip('"\''))
    required = ('STREAMTAPE_LOGIN', 'STREAMTAPE_KEY') + (('WEBTOR_API_KEY',) if require_webtor else ())
    missing = [key for key in required if not os.environ.get(key)]
    if missing:
        raise ValueError('Faltan variables: ' + ', '.join(missing))
    if not require_webtor:
        return 'https://api.webtor.io/v1'
    endpoint = os.environ.get('WEBTOR_ENDPOINT', 'https://api.webtor.io/v1')
    endpoint = endpoint.replace('\\:', ':').rstrip('\\/ ')
    if endpoint != 'https://api.webtor.io/v1':
        raise ValueError('WEBTOR_ENDPOINT debe ser https://api.webtor.io/v1')
    return endpoint


class APIError(RuntimeError):
    pass


class Client:
    def __init__(self, endpoint):
        self.endpoint = endpoint
        self.context = ssl.create_default_context(cafile=certifi.where())

    def request(self, url, data=None, headers=None, timeout=45):
        try:
            with urlopen(Request(url, data=data, headers=headers or {}),
                         timeout=timeout, context=self.context) as response:
                return json.load(response)
        except HTTPError as error:
            # Never include URLs, request bodies, or signed download links in errors.
            raise APIError(f'HTTP {error.code}') from None
        except (URLError, TimeoutError, OSError, ValueError):
            raise APIError('Respuesta de red ausente o inválida') from None

    def streamtape(self, path, **params):
        params.update(login=os.environ['STREAMTAPE_LOGIN'], key=os.environ['STREAMTAPE_KEY'])
        data = self.request('https://api.streamtape.com' + path, urlencode(params).encode())
        if data.get('status') != 200:
            raise APIError(f'Streamtape {path}: estado {data.get("status")}')
        return data.get('result') or {}

    def download(self, magnet):
        headers = {'Authorization': 'Bearer ' + os.environ['WEBTOR_API_KEY'],
                   'Content-Type': 'application/octet-stream'}
        resource = self.request(self.endpoint + '/resource/', magnet.encode(), headers, 240)
        file = resource.get('file')
        if not file:
            paths, visited, items = ['/'], set(), []
            while paths:
                path = paths.pop()
                if path in visited:
                    continue
                visited.add(path)
                listing = self.request(self.endpoint + '/resource/' + resource['id'] + '/list?'
                                       + urlencode({'path': path, 'limit': 1000}), headers=headers)
                batch = listing.get('items', [])
                if listing.get('items_count', len(batch)) > len(batch):
                    raise APIError('Listado de torrent incompleto: requiere selección manual')
                items.extend(batch)
                paths.extend(item['path'] for item in batch if item.get('type') == 'directory')
            # Do not silently pick an episode, sample, or unrelated movie from a pack.
            files = [item for item in items
                     if item.get('type') == 'file' and item.get('media_format') == 'video'
                     and not re.search(r'\b(sample|trailer)\b', item.get('name', ''), re.I)]
            if len(files) != 1:
                raise APIError('Torrent con varios archivos: requiere selección manual')
            file = files[0]
        if file.get('media_format') != 'video':
            raise APIError('El archivo del torrent no es un video')
        result = self.request(self.endpoint + '/resource/' + resource['id'] + '/export/'
                              + file['id'] + '?types=download', headers=headers)
        url = result.get('exports', {}).get('download', {}).get('url')
        if not url or urlparse(url).scheme not in ('https', 'http'):
            raise APIError('Webtor no devolvió una URL de descarga')
        return url


def language_rank(option):
    language = option.get('language', '').casefold()
    if 'latino' in language or 'latin' in language:
        return 0
    if any(word in language for word in ('español', 'espanol', 'castellano', 'spanish')):
        return 1
    return 2


def pick_magnet(movie):
    options = [item for item in movie.get('magnets', [])
               if str(item.get('magnet', '')).startswith('magnet:?')]
    return min(options, key=language_rank) if options else None


def info_hash(option):
    if option.get('info_hash'):
        return option['info_hash'].lower()
    values = parse_qs(urlparse(option.get('magnet', '')).query).get('xt', [])
    return next((value[9:].lower() for value in values if value.startswith('urn:btih:')), '')


def normalized(value):
    value = unicodedata.normalize('NFKD', str(value)).casefold()
    return ' '.join(re.findall(r'[^\W_]+', ''.join(c for c in value if not unicodedata.combining(c))))


def title_match(movie, filename):
    name = normalized(filename)
    year = str(movie.get('year') or '')
    if not re.fullmatch(r'\d{4}', year):
        return False
    for title in (movie.get('title'), movie.get('original_title')):
        prefix = normalized(title or '') + ' ' + year
        if title and (name == prefix or name.startswith(prefix + ' ')):
            return True
    return False


def database(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.execute('''CREATE TABLE IF NOT EXISTS movies (
        tmdb_id INTEGER PRIMARY KEY, title TEXT, status TEXT NOT NULL,
        remote_id TEXT, file_id TEXT, language TEXT, info_hash TEXT,
        error TEXT, updated_at TEXT NOT NULL)''')
    db.execute('''CREATE TABLE IF NOT EXISTS remote_files (
        file_id TEXT PRIMARY KEY, tmdb_id INTEGER, name TEXT, folder_id TEXT)''')
    return db


def remember(db, tmdb_id, **fields):
    fields['updated_at'] = datetime.now(timezone.utc).isoformat()
    columns = ['tmdb_id', *fields]
    db.execute('INSERT INTO movies (' + ','.join(columns) + ') VALUES ('
               + ','.join('?' for _ in columns) + ') ON CONFLICT(tmdb_id) DO UPDATE SET '
               + ','.join(key + '=excluded.' + key for key in fields), [tmdb_id, *fields.values()])
    db.commit()


@contextmanager
def exclusive(path):
    with portable_exclusive(path):
        yield


def sync_files(client, db, movies):
    pending, visited, files = [''], set(), []
    while pending:
        folder = pending.pop()
        if folder in visited:
            continue
        visited.add(folder)
        listing = client.streamtape('/file/listfolder', **({'folder': folder} if folder else {}))
        for item in listing.get('files', []):
            item = dict(item, folder_id=folder)
            files.append(item)
        pending.extend(item['id'] for item in listing.get('folders', []))
    # Replace the snapshot only after the full account query succeeds.
    with db:
        db.execute('DELETE FROM remote_files')
        for item in files:
            match = TMDB.search(item.get('name', ''))
            tmdb_id = int(match.group(1)) if match else None
            db.execute('INSERT OR REPLACE INTO remote_files VALUES (?,?,?,?)',
                       (item['linkid'], tmdb_id, item.get('name', ''), item['folder_id']))
    tagged = {int(row['tmdb_id']): row['file_id'] for row in db.execute(
        'SELECT tmdb_id,file_id FROM remote_files WHERE tmdb_id IS NOT NULL')}
    untagged = [item for item in files if not TMDB.search(item.get('name', ''))]
    for movie in movies:
        tmdb_id = movie.get('tmdb_id')
        if not isinstance(tmdb_id, int) or tmdb_id <= 0:
            continue
        file_id = tagged.get(tmdb_id)
        if not file_id:
            found = next((item for item in untagged if title_match(movie, item.get('name', ''))), None)
            file_id = found['linkid'] if found else None
        if file_id:
            remember(db, tmdb_id, title=movie.get('title'), status='existing', file_id=file_id, error=None)
    print(f'Streamtape: {len(files)} archivos consultados en {len(visited)} carpetas.', flush=True)


def sync_queue(client, db, movies):
    jobs = client.streamtape('/remotedl/status', limit=1000)
    if len(jobs) >= 1000:
        raise APIError('La consulta de cola alcanzó 1000 trabajos; revisar antes de enviar más')
    by_hash = {}
    for movie in movies:
        if isinstance(movie.get('tmdb_id'), int) and movie['tmdb_id'] > 0:
            for option in movie.get('magnets', []):
                key = info_hash(option)
                if key:
                    by_hash.setdefault(key, []).append(movie)
    for job in jobs.values():
        if str(job.get('status', '')).lower() in ('error', 'failed'):
            continue
        for key in re.findall(r'(?<![0-9a-f])[0-9a-f]{40}(?![0-9a-f])', job.get('remoteurl', '').lower()):
            for movie in by_hash.get(key, []):
                existing = db.execute('SELECT status FROM movies WHERE tmdb_id=?', (movie['tmdb_id'],)).fetchone()
                if existing and existing['status'] in ('existing', 'completed'):
                    continue
                remember(db, movie['tmdb_id'], title=movie.get('title'), status='queued',
                         remote_id=job['id'], info_hash=key, error=None)
    # A missing remote job is not evidence of failure: never automatically resubmit it.
    for row in list(db.execute("SELECT * FROM movies WHERE remote_id IS NOT NULL AND status NOT IN ('existing','completed')")):
        try:
            response = client.streamtape('/remotedl/status', id=row['remote_id'])
        except APIError:
            continue
        job = response.get(row['remote_id'])
        if not job:
            continue
        if job.get('linkid'):
            remember(db, row['tmdb_id'], status='completed', file_id=job['linkid'], error=None)
        elif str(job.get('status', '')).lower() in ('error', 'failed'):
            remember(db, row['tmdb_id'], status='failed', error='Streamtape informó un fallo')
        elif row['status'] != 'queued':
            remember(db, row['tmdb_id'], status='queued', error=None)


def run(args, client, db):
    document = json.loads(args.input.read_text(encoding='utf-8'))
    if document.get('media_type', 'movie') != 'movie':
        raise ValueError('Este script solo procesa películas')
    movies = document['movies']  # A snapshot; the scraper may atomically replace it while we work.
    sync_files(client, db, movies)
    sync_queue(client, db, movies)
    submitted, attempted, seen = 0, 0, set()
    for movie in movies:
        tmdb_id = movie.get('tmdb_id')
        if not isinstance(tmdb_id, int) or tmdb_id <= 0 or tmdb_id in seen:
            continue
        seen.add(tmdb_id)
        previous = db.execute('SELECT status FROM movies WHERE tmdb_id=?', (tmdb_id,)).fetchone()
        if previous and not (args.retry_failed and previous['status'] == 'failed'):
            continue
        option = pick_magnet(movie)
        if not option:
            continue
        attempted += 1
        if args.dry_run:
            print(f'Pendiente TMDB:{tmdb_id} · {movie.get("title")} · {option.get("language")}', flush=True)
        else:
            remember(db, tmdb_id, title=movie.get('title'), status='preparing',
                     language=option.get('language'), info_hash=info_hash(option), error=None)
            try:
                url = client.download(option['magnet'])
            except Exception as error:
                reason = str(error) if isinstance(error, APIError) else type(error).__name__
                remember(db, tmdb_id, status='failed', error=reason)
                print(f'TMDB:{tmdb_id}: Webtor: {reason}; guardado para reintento.', flush=True)
                if attempted >= args.limit:
                    break
                continue
            # Commit before the non-idempotent request. Any ambiguous outcome stays blocked.
            remember(db, tmdb_id, status='submitting')
            name = f'TMDB:{tmdb_id} - {movie.get("title")} ({movie.get("year")}) - {option.get("language")}.mp4'
            params = {'url': url, 'name': name}
            if args.folder_id:
                params['folder'] = args.folder_id
            try:
                result = client.streamtape('/remotedl/add', **params)
                if not result.get('id'):
                    raise APIError('Streamtape no devolvió un ID')
            except Exception as error:
                reason = str(error) if isinstance(error, APIError) else type(error).__name__
                remember(db, tmdb_id, status='uncertain', error=reason)
                print(f'TMDB:{tmdb_id}: respuesta incierta ({reason}); no se reenviará automáticamente.', flush=True)
                break
            remember(db, tmdb_id, status='queued', remote_id=result['id'], error=None)
            submitted += 1
            print(f'En cola TMDB:{tmdb_id} · {movie.get("title")} · {option.get("language")} · {result["id"]}', flush=True)
        if attempted >= args.limit:
            break
    totals = dict(Counter(row['status'] for row in db.execute('SELECT status FROM movies')))
    print(f'Enviadas ahora: {submitted} · procesadas: {attempted} · registro local: {json.dumps(totals)}', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, default=ROOT / 'catalog/PeliPanda.movie.json')
    parser.add_argument('--state', type=Path, default=ROOT / 'output/pelipanda_streamtape.sqlite3')
    parser.add_argument('--limit', type=int, default=10)
    parser.add_argument('--folder-id', help='Carpeta destino; por defecto raíz de Streamtape')
    parser.add_argument('--dry-run', action='store_true', help='Sincronizar y mostrar candidatas sin enviarlas')
    parser.add_argument('--retry-failed', action='store_true', help='Permitir reintentos de fallos confirmados')
    args = parser.parse_args()
    if args.limit < 1:
        parser.error('--limit debe ser positivo')
    endpoint = credentials()
    with exclusive(args.state.with_suffix('.lock')):
        with database(args.state) as db:
            # preparing has not yet invoked Streamtape, so interrupted preparation is safe to retry.
            db.execute("UPDATE movies SET status='failed' WHERE status='preparing'")
            db.commit()
            run(args, Client(endpoint), db)


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print('\nInterrumpido. Se conserva el registro de solicitudes enviadas.', flush=True)
        raise SystemExit(130)
    except (APIError, ValueError, sqlite3.Error) as error:
        print(f'Error: {error}', flush=True)
        raise SystemExit(1)
