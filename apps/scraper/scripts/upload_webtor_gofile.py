#!/usr/bin/env python3
"""Stream original Webtor movie files to GoFile without conversion or local videos."""
import argparse
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
import hashlib
import json
import mimetypes
import threading
import requests
import os
from pathlib import Path
import time
import uuid

from scrape_webtor_movies import OUTPUT, BAD_RELEASE, MAX_MOVIE_BYTES, allowed_size, Webtor, load_key, read_catalog, source_rank, now
from upload_cineari_gofile import GoFile, safe_name
from upload_movies_b2 import load_local_env
from portable_file_lock import exclusive
from gofile_inventory import collect_inventory, find_existing
from terminal_dashboard import Dashboard, NullDashboard
from webtor_compatible_mp4 import PROFILE, LEGACY_PROFILES, FinalizedVideo, command, ffmpeg_binary, probe, validate_tools, stop_all

STATE = OUTPUT.parent / 'webtor-gofile-mp4-state.json'
RAW_PROFILE = 'webtor-original-v1'
_active_originals = set()
_original_lock = threading.Lock()
_original_stopping = threading.Event()


def output_name(movie):
    return f'{movie["tmdb_id"]} - {safe_name(movie["title"])} - MULTI H264-AAC.mp4'


def original_name(movie, source):
    filename = source['filename'].replace('\\', '/').rsplit('/', 1)[-1]
    suffix = Path(filename).suffix
    if not suffix or len(suffix) > 12 or not suffix[1:].isalnum():
        raise ValueError('El origen no tiene una extensión de archivo válida')
    return f'{movie["tmdb_id"]} - {safe_name(movie["title"])[:80]} - {safe_name(Path(filename).stem)[:100]}{suffix}'


class OriginalStream:
    """Read the original HTTP body, with no decoding, transcoding or disk file."""
    def __init__(self, url, expected_size):
        if _original_stopping.is_set():
            raise ValueError('Transferencia detenida por el usuario')
        self.response = requests.get(url, headers={'Accept-Encoding': 'identity'}, stream=True, timeout=(30, 60))
        try:
            self.response.raise_for_status()
            if self.response.status_code != 200:
                raise ValueError('El origen no devolvió el archivo completo')
            content_type = self.response.headers.get('Content-Type', '').lower()
            if 'text/' in content_type or 'json' in content_type:
                raise ValueError('El origen devolvió una página o error, no el archivo')
            if self.response.headers.get('Content-Encoding', 'identity').lower() != 'identity':
                raise ValueError('El origen cambió la codificación HTTP del archivo')
            length = int(self.response.headers.get('Content-Length') or 0)
            if length and length != expected_size:
                raise ValueError('El tamaño HTTP no coincide con el archivo de Webtor')
            self.size = expected_size
            self.received = 0
            self.eof = False
            with _original_lock:
                if _original_stopping.is_set():
                    raise ValueError('Transferencia detenida por el usuario')
                _active_originals.add(self)
        except BaseException:
            self.response.close()
            raise

    def read(self, size):
        if _original_stopping.is_set():
            raise ValueError('Transferencia detenida por el usuario')
        reader = getattr(self.response.raw, 'read1', None)
        data = reader(size, decode_content=False) if reader else self.response.raw.read(min(size, 65536), decode_content=False)
        self.received += len(data)
        if self.received > self.size:
            raise ValueError('El origen envió más bytes de los anunciados')
        if not data:
            self.eof = True
        return data

    def verify(self):
        if not self.eof or self.received != self.size:
            raise ValueError('La descarga del original quedó incompleta')

    def close(self):
        self.response.close()
        with _original_lock:
            _active_originals.discard(self)


def movies(document, allow_large_source=True):
    grouped = {}
    for movie in document['movies']:
        tid = movie.get('tmdb_id')
        if not isinstance(tid, int) or tid <= 0:
            continue
        row = grouped.setdefault(tid, {**movie, 'sources': []})
        row['sources'].extend(source for source in movie.get('sources', [])
            if source.get('resolution') == '1080p'
            and allowed_size(source.get('size_bytes'), float('inf') if allow_large_source else MAX_MOVIE_BYTES)
            and any(track.get('spanish') for track in source.get('audio', []))
            and not BAD_RELEASE.search(source.get('filename', '')))
    return [row for row in grouped.values() if row['sources']]


class Multipart:
    def __init__(self, stream, filename, folder, chunk, label, progress=None, max_bytes=MAX_MOVIE_BYTES, content_type='video/mp4', stage='MP4/envio'):
        self.max_bytes, self.stage = max_bytes, stage
        self.stream, self.chunk, self.label = stream, chunk, label
        self.progress = progress
        self.boundary = 'webtor-' + uuid.uuid4().hex
        self.prefix = (f'--{self.boundary}\r\nContent-Disposition: form-data; name="folderId"\r\n\r\n{folder}\r\n'
                       f'--{self.boundary}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\n'
                       f'Content-Type: {content_type}\r\n\r\n').encode()
        self.suffix = f'\r\n--{self.boundary}--\r\n'.encode()
        self.sent = 0
        self.md5 = hashlib.md5()
        self.complete = False

    def __iter__(self):
        yield self.prefix
        started = last = time.monotonic()
        while data := self.stream.read(self.chunk):
            if self.max_bytes is not None and self.sent + len(data) > self.max_bytes:
                raise ValueError('El MP4 supera el máximo de 5 GB; subida interrumpida')
            self.sent += len(data)
            self.md5.update(data)
            yield data
            if self.progress and time.monotonic() - last >= 0.25:
                elapsed = time.monotonic() - started
                size = getattr(self.stream, 'size', None)
                exact = isinstance(size, int) and size > 0
                ratio = min(0.999, max(0, self.sent / size if exact else self.stream.seconds / self.stream.duration))
                estimated = size if exact else self.sent / ratio if ratio > 0.005 else None
                self.progress(stage=self.stage, percent=ratio*100, sent=self.sent,
                    total=estimated, estimated=not exact, speed=self.sent/1048576/max(elapsed,0.001),
                    eta=elapsed*(1-ratio)/ratio if ratio else None)
                last = time.monotonic()
        self.stream.verify()
        if not self.sent:
            raise ValueError('El origen no entregó datos')
        self.complete = True
        if self.progress:
            self.progress(stage='Confirmando', percent=99.9, sent=self.sent, total=self.sent,
                          estimated=False, speed=0, eta=None)
        yield self.suffix


def transfer(movie, key, folder, chunk, binary=None, encoder_threads=2, progress=None, allow_large_source=True):
    tid = movie['tmdb_id']
    submitted = False
    video = body = None
    def report(**fields):
        if progress:
            progress(**fields)
    try:
        report(stage='Webtor')
        # Separate sessions per worker; refresh signed downloads just before reading.
        source_limit = float('inf') if allow_large_source else MAX_MOVIE_BYTES
        webtor = Webtor(key, max_source_bytes=source_limit)
        options = sorted((source for source in movie['sources'] if allowed_size(source.get('size_bytes'), source_limit)), key=source_rank)
        download = None
        for source in options:
            try:
                download = webtor.refresh(source)
                if not allowed_size(download.get('size_bytes'), source_limit):
                    download = None
                    continue
                break
            except Exception:
                continue
        if download is None:
            raise ValueError('Ninguna fuente pudo renovarse en Webtor')
        client = GoFile()
        report(stage='Verificando GoFile')
        existing = find_existing(client, folder, tid)
        if existing:
            return tid, {'status': 'exists', 'file': existing, 'title': movie['title'], 'updated_at': now()}
        report(stage='Conectando origen')
        video = OriginalStream(download['download_url'], download['size_bytes'])
        filename = original_name(movie, download)
        body = Multipart(video, filename, folder, chunk, f'TMDB {tid}', progress,
                         max_bytes=None, content_type=mimetypes.guess_type(filename)[0] or 'application/octet-stream',
                         stage='Original/envio')
        report(stage='Original/envio', percent=0, sent=0, total=video.size,
               estimated=False, speed=0, eta=None)
        submitted = True
        response = client.session.post('https://upload.gofile.io/uploadfile', data=body,
            headers={'Content-Type': 'multipart/form-data; boundary=' + body.boundary}, timeout=(30, 300))
        response.raise_for_status()
        result = response.json()
        data = result.get('data') or {}
        if (result.get('status') != 'ok' or data.get('parentFolder') != folder
                or not body.complete or data.get('size') != body.sent
                or data.get('md5') != body.md5.hexdigest()):
            raise ValueError('GoFile no confirmó destino, tamaño y MD5 correctos')
        return tid, {'status': 'uploaded', 'profile': RAW_PROFILE, 'source_filename': download['filename'],
                     'file': data, 'title': movie['title'], 'updated_at': now()}
    except Exception as error:
        # Signed source URLs and API tokens must not appear in logs or state.
        return tid, {'status': 'uncertain' if submitted else 'failed', 'title': movie['title'],
                     'error_type': type(error).__name__,
                     'message': str(error) if type(error) is ValueError else 'Error de red o del proceso multimedia',
                     'updated_at': now()}
    finally:
        if video is not None:
            video.close()


def reconcile(state, inventory, retry_after=None, active=()):
    grouped = {}
    for item in inventory:
        if item['media_kind'] == 'movie':
            grouped.setdefault(str(item['tmdb_id']), []).append(item)
    for tid, items in grouped.items():
        previous = state['movies'].get(tid, {})
        if previous.get('status') == 'uploaded' and any(previous.get('file', {}).get('id') == item['id'] for item in items):
            continue
        state['movies'][tid] = {'status': 'exists', 'file': items[0],
            'reason': 'remote_video_exists_for_tmdb', 'updated_at': now()}

    if retry_after is not None:
        active = set(active)
        for tid, previous in state['movies'].items():
            if tid in active or tid in grouped or previous.get('status') not in ('processing', 'uncertain'):
                continue
            previous['status'] = 'uncertain'
            try:
                age = time.time() - datetime.fromisoformat(previous['updated_at']).timestamp()
            except (ValueError, KeyError):
                age = retry_after
            if age < retry_after:
                continue
            previous['absence_checks'] = previous.get('absence_checks', 0) + 1
            previous['absence_checked_at'] = now()
            if previous['absence_checks'] >= 2:
                previous.update(status='failed', message='Ausente en dos inventarios nuevos de GoFile; se reintentará')
                # A stale processing record may have had no timestamp at all.
                previous.setdefault('updated_at', datetime.fromtimestamp(time.time() - retry_after - 1).astimezone().isoformat())


def eligible(rows, state, attempted, retry_failed, continuous, retry_after):
    pending = []
    for movie in rows:
        tid = str(movie['tmdb_id'])
        previous = state['movies'].get(tid, {})
        status = previous.get('status')
        retry = retry_failed and tid not in attempted
        if continuous and status == 'failed':
            try:
                age = time.time() - datetime.fromisoformat(previous['updated_at']).timestamp()
                retry = retry or age >= retry_after
            except (ValueError, KeyError):
                retry = True
        if not status or (status == 'failed' and retry):
            pending.append(movie)
    return sorted(pending, key=lambda movie: source_rank(min(movie['sources'], key=source_rank)))


def run_queue(args, state, client, key, folder, save, panel=None, catalog_loader=None, uploader=None):
    panel = panel or NullDashboard()
    retrying = uploader is None and not args.once
    continuous = not args.once and (not args.limit or retrying)
    attempted = set()
    attempt_count = failures = successful = 0
    rows = []
    inventory_ok = False
    next_scan = next_inventory = 0.0
    idle = False
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        tasks = {}
        slots = {}
        try:
            while True:
                for task in list(tasks):
                    if not task.done():
                        continue
                    tid = tasks.pop(task)
                    slot = slots.pop(task)
                    try:
                        _, result = task.result()
                    except Exception as error:
                        result = {'status': 'uncertain', 'error_type': type(error).__name__, 'updated_at': now()}
                    result.setdefault('updated_at', now())
                    state['movies'][tid] = result
                    successful += result['status'] == 'uploaded'
                    if retrying and result['status'] == 'uncertain':
                        next_inventory = min(next_inventory, time.monotonic() + args.poll_seconds)
                    save()
                    failures += result['status'] not in ('uploaded', 'exists')
                    panel.finish(slot, result)
                if retrying and args.limit and successful >= args.limit and not tasks:
                    return 0
                clock = time.monotonic()
                if clock >= next_scan:
                    try:
                        rows = catalog_loader(args.input) if catalog_loader else movies(read_catalog(args.input), getattr(args, 'allow_large_source', True))
                    except (ValueError, OSError):
                        panel.phase('Catalogo temporalmente ilegible; esperando reintento')
                        if not continuous and not tasks:
                            return 1
                    next_scan = clock + args.poll_seconds if continuous else float('inf')
                if clock >= next_inventory:
                    panel.phase('Sincronizando inventario de GoFile...')
                    try:
                        inventory = collect_inventory(client, folder)
                        reconcile(state, inventory, args.retry_after_seconds if retrying else None, tasks.values())
                        state['inventory_checked_at'] = now()
                        state['remote_inventory'] = inventory
                        save()
                        panel.phase(f'Inventario sincronizado: {len(inventory)} archivos | Procesando pendientes')
                        inventory_ok = True
                        next_inventory = clock + args.inventory_seconds
                        if retrying and any(r.get('status') == 'uncertain' for r in state['movies'].values()):
                            next_inventory = min(next_inventory, clock + args.poll_seconds)
                    except Exception as error:
                        inventory_ok = False
                        next_inventory = clock + args.poll_seconds
                        panel.phase(f'GoFile no disponible ({type(error).__name__}); esperando reintento')
                        if not continuous and not tasks:
                            return 1
                pending = eligible(rows, state, attempted, args.retry_failed, continuous, args.retry_after_seconds)
                while inventory_ok and pending and len(tasks) < args.workers and (not args.limit or (successful + len(tasks) if retrying else attempt_count) < args.limit):
                    movie = pending.pop(0)
                    tid = str(movie['tmdb_id'])
                    attempted.add(tid)
                    attempt_count += 1
                    state['movies'][tid] = {'status': 'processing', 'title': movie['title'], 'updated_at': now()}
                    save()
                    slot = next(index for index in range(args.workers) if index not in slots.values())
                    panel.start(slot, movie['title'])
                    task = pool.submit(uploader or transfer, movie, key, folder, args.buffer_mib * 1048576,
                        args.ffmpeg, args.encoder_threads, lambda _slot=slot, **fields: panel.update(_slot, **fields),
                        getattr(args, 'allow_large_source', True))
                    tasks[task] = tid
                    slots[task] = slot
                    idle = False
                if not tasks:
                    if not continuous:
                        return 1 if failures else 0
                    if not idle:
                        reason = 'Catalogo sin pendientes nuevos' if inventory_ok else 'Esperando poder verificar GoFile'
                        panel.phase(f'{reason} | nueva revision en {args.poll_seconds}s')
                        idle = True
                    if retrying:
                        current_ids = {str(row['tmdb_id']) for row in rows}
                        cooling = [r for tid, r in state['movies'].items() if tid in current_ids
                                   and r.get('status') in ('failed', 'uncertain', 'processing')]
                        if cooling:
                            check_at = min(next_scan, next_inventory)
                            for record in cooling:
                                if record.get('status') == 'failed':
                                    try:
                                        age = time.time() - datetime.fromisoformat(record['updated_at']).timestamp()
                                        check_at = min(check_at, clock + max(0, args.retry_after_seconds - age))
                                    except (ValueError, KeyError):
                                        check_at = clock
                            remaining = max(1, int(check_at - clock + 0.999))
                            panel.phase(f'{len(cooling)} por reintentar | comprobacion en {remaining}s | Ctrl+C para detener')
                delay = max(0.1, min(next_scan, next_inventory) - time.monotonic())
                if tasks:
                    wait(tasks, timeout=min(delay, 1 if retrying else 60), return_when=FIRST_COMPLETED)
                else:
                    time.sleep(min(delay, 1 if retrying else 60))
        except KeyboardInterrupt:
            panel.phase('Detenido por el usuario; progreso guardado')
            if uploader is None:
                _original_stopping.set()
                with _original_lock:
                    active = list(_active_originals)
                for stream in active:
                    stream.close()
            else:
                stop_all()
            for task, tid in tasks.items():
                task.cancel()
                state['movies'][tid] = {'status': 'uncertain', 'reason': 'interrupted', 'updated_at': now()}
            save()
            return 130


def main(argv=None, *, catalog_loader=None, uploader=None, default_input=OUTPUT, default_state=STATE, require_webtor=True, description=None):
    parser = argparse.ArgumentParser(description=description or __doc__)
    parser.add_argument('--input', type=Path, default=default_input)
    parser.add_argument('--state', type=Path, default=default_state)
    parser.add_argument('--workers', type=int, default=1, help='Peliculas simultaneas')
    parser.add_argument('--limit', type=int, default=0, help='0 = continuo; Webtor cuenta subidas exitosas y reintenta hasta completarlas; --once cuenta intentos')
    parser.add_argument('--once', action='store_true', help='Procesar las pendientes y salir, sin esperar peliculas nuevas')
    parser.add_argument('--poll-seconds', type=float, default=60)
    parser.add_argument('--inventory-seconds', type=float, default=300)
    parser.add_argument('--retry-after-seconds', type=float, default=60 if require_webtor else 1800, help='Espera entre reintentos; Webtor reintenta sin límite salvo --once')
    parser.add_argument('--buffer-mib', type=int, default=1)
    parser.add_argument('--ffmpeg')
    parser.add_argument('--encoder-threads', type=int, default=2)
    parser.add_argument('--folder-id')
    parser.add_argument('--retry-failed', action='store_true')
    parser.add_argument('--allow-large-source', action='store_true', default=True, help='Compatibilidad: las fuentes grandes ya se permiten por defecto; se envía el original')
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--sync-only', action='store_true', help='Actualizar inventario local de GoFile sin convertir ni subir')
    args = parser.parse_args(argv)
    if min(args.workers, args.buffer_mib, args.encoder_threads, args.poll_seconds, args.inventory_seconds, args.retry_after_seconds) <= 0 or args.limit < 0:
        parser.error('Workers, tiempos y buffer deben ser positivos; limite no negativo')
    load_local_env()
    with exclusive(args.state.with_suffix('.lock')):
        state = json.loads(args.state.read_text()) if args.state.exists() else {'movies': {}}
        if any(row.get('status') == 'uploaded' and row.get('profile') not in ({PROFILE, RAW_PROFILE} | LEGACY_PROFILES) for row in state['movies'].values()):
            parser.error('El estado contiene un perfil desconocido; revisa el archivo --state')
        if args.dry_run:
            rows = catalog_loader(args.input) if catalog_loader else movies(read_catalog(args.input), args.allow_large_source)
            pending = eligible(rows, state, set(), args.retry_failed, False, args.retry_after_seconds)
            pending = pending[:args.limit] if args.limit else pending
            for movie in pending:
                print(f'{movie["tmdb_id"]} - {movie["title"]}')
            print(f'{len(pending)} pendientes locales; workers={args.workers}; sin subidas ni espera.', flush=True)
            return 0
        if not os.environ.get('GOFILE_TOKEN'):
            parser.error('Falta GOFILE_TOKEN en .env.local')
        with Dashboard(args.workers, title='GoFile | Originales de Webtor' if require_webtor else 'GoFile | MP4 H.264 + AAC multiaudio', byte_progress=require_webtor) as panel:
            if not args.sync_only and not require_webtor:
                validate_tools(ffmpeg_binary(args.ffmpeg))
            key = load_key() if require_webtor and not args.sync_only else None
            client = GoFile()
            folder = args.folder_id or state.get('folder_id')
            while not folder:
                try:
                    account = client.api('GET', 'accounts/getid')['id']
                    folder = client.api('GET', 'accounts/' + account)['rootFolder']
                except Exception as error:
                    if args.once or (args.limit and not require_webtor) or args.sync_only:
                        raise
                    panel.phase(f'GoFile no disponible ({type(error).__name__}); reintentando en {args.poll_seconds}s')
                    time.sleep(args.poll_seconds)
            if state.get('folder_id') not in (None, folder):
                raise ValueError('Este estado pertenece a otra carpeta; usa --state diferente')
            state['folder_id'] = folder

            def save():
                temporary = args.state.with_suffix('.tmp')
                temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2) + '\n')
                temporary.chmod(0o600)
                temporary.replace(args.state)

            save()
            if args.sync_only:
                inventory = collect_inventory(client, folder)
                reconcile(state, inventory)
                state['inventory_checked_at'] = now()
                state['remote_inventory'] = inventory
                save()
                panel.phase(f'Inventario actualizado: {len(inventory)} archivos; sin subidas')
                return 0
            return run_queue(args, state, client, key, folder, save, panel, catalog_loader, uploader)


if __name__ == '__main__':
    raise SystemExit(main())
