#!/usr/bin/env python3
"""Upload a chosen local video folder to GoFile without changing the originals."""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
import mimetypes
import os
from pathlib import Path
import re
import sys

import requests
from gofile_inventory import collect_inventory, VIDEO_EXTENSIONS
from portable_file_lock import exclusive
from terminal_dashboard import Dashboard
from upload_cineari_gofile import GoFile, safe_name
from upload_movies_b2 import load_local_env
from upload_webtor_gofile import Multipart

ROOT = Path(__file__).resolve().parents[3]


def error_text(error):
    if isinstance(error, requests.HTTPError):
        return f'GoFile HTTP {error.response.status_code}'
    if isinstance(error, requests.RequestException):
        return f'Red/TLS: {type(error).__name__}'
    return re.sub(r'https?://\S+', '[URL]', str(error))[:300]


def videos(folder):
    return sorted(p for p in folder.rglob('*') if p.is_file() and not p.is_symlink()
                  and p.suffix.lower() in VIDEO_EXTENSIONS)


def digest(path):
    value = hashlib.md5()
    with path.open('rb') as file:
        while chunk := file.read(1024 * 1024):
            value.update(chunk)
    return value.hexdigest()


def existing_file(inventory, name, size, checksum):
    return next((row for row in inventory if row.get('name') == name
                 and int(row.get('size') or 0) == size and row.get('md5') == checksum), None)


class LocalStream:
    def __init__(self, path, size):
        self.file = path.open('rb')
        self.size, self.received = size, 0

    def read(self, count):
        data = self.file.read(count)
        self.received += len(data)
        return data

    def verify(self):
        if self.received != self.size:
            raise ValueError('El archivo cambio de tamano durante la lectura')

    def close(self):
        self.file.close()


def transfer(path, folder, size, checksum, progress):
    stream = LocalStream(path, size)
    try:
        body = Multipart(stream, safe_name(path.name), folder, 1024 * 1024, path.name,
                         progress, max_bytes=None,
                         content_type=mimetypes.guess_type(path.name)[0] or 'application/octet-stream',
                         stage='Original/envio')
        progress(stage='Original/envio', percent=0, sent=0, total=size, estimated=False)
        client = GoFile()
        response = client.session.post('https://upload.gofile.io/uploadfile', data=body,
            headers={'Content-Type': 'multipart/form-data; boundary=' + body.boundary}, timeout=(30, 300))
        response.raise_for_status()
        result = response.json()
        data = result.get('data') or {}
        if (result.get('status') != 'ok' or data.get('parentFolder') != folder
                or not body.complete or data.get('size') != size or data.get('md5') != checksum
                or body.md5.hexdigest() != checksum):
            raise ValueError('GoFile no confirmo destino, tamano y MD5 correctos')
        return {'status': 'uploaded', 'file': data}
    finally:
        stream.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path)
    parser.add_argument('--workers', type=int, default=1)
    parser.add_argument('--limit', type=int, default=0)
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--folder-id')
    parser.add_argument('--state', type=Path, default=ROOT / 'apps/scraper/output/local-gofile-state.json')
    args = parser.parse_args()
    if args.workers < 1 or args.limit < 0:
        parser.error('Workers debe ser positivo; limit >= 0')
    folder = args.directory
    if folder is None:
        selected = input('Carpeta donde estan las peliculas (incluye subcarpetas): ').strip().strip('"')
        if not selected:
            parser.error('Indica una carpeta; no se selecciona ninguna automaticamente')
        folder = Path(selected)
    folder = folder.expanduser().resolve()
    if not folder.is_dir():
        parser.error('La carpeta indicada no existe')
    paths = videos(folder)
    print(f'{len(paths)} archivos de video encontrados en {folder}', flush=True)
    if args.dry_run:
        for path in paths[:args.limit or None]:
            print(path.relative_to(folder))
        print('Comprobacion terminada; no se subio ningun archivo.')
        return 0
    if not paths:
        return 0
    load_local_env()
    if not os.environ.get('GOFILE_TOKEN'):
        parser.error('Falta GOFILE_TOKEN en .env.local; este modo no requiere Webtor')
    args.state.parent.mkdir(parents=True, exist_ok=True)
    with exclusive(args.state.with_suffix('.lock')):
        state = json.loads(args.state.read_text()) if args.state.exists() else {'files': {}}

        def save():
            tmp = args.state.with_suffix('.tmp')
            tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding='utf-8')
            tmp.replace(args.state)

        client = GoFile()
        try:
            destination = args.folder_id or state.get('folder_id')
            if not destination:
                account = client.api('GET', 'accounts/getid')['id']
                destination = client.api('GET', 'accounts/' + account)['rootFolder']
            if state.get('folder_id') not in (None, destination):
                raise ValueError('El estado pertenece a otro destino; usa otro archivo --state')
            print('Verificando inventario de GoFile...', flush=True)
            inventory = collect_inventory(client, destination)
        except Exception as error:
            state['inventory_error'] = error_text(error)
            save()
            print('No se puede verificar GoFile: ' + state['inventory_error'], file=sys.stderr)
            return 1
        state['folder_id'] = destination
        state.pop('inventory_error', None)
        save()
        failures = 0
        submitted = 0
        with Dashboard(args.workers, title='GoFile | Peliculas locales', byte_progress=True) as panel:
            with ThreadPoolExecutor(max_workers=args.workers) as pool:
                # Hash and submit only one bounded batch at a time.
                iterator = iter(paths)
                exhausted = False
                while not exhausted and (not args.limit or submitted < args.limit):
                    tasks = {}
                    for slot in range(args.workers):
                        if args.limit and submitted >= args.limit:
                            break
                        while True:
                            path = next(iterator, None)
                            if path is None:
                                exhausted = True
                                break
                            key = str(path)
                            size = path.stat().st_size
                            panel.start(slot, path.name)
                            panel.update(slot, stage='Verificando MD5')
                            checksum = digest(path)
                            existing = existing_file(inventory, safe_name(path.name), size, checksum)
                            if existing:
                                state['files'][key] = {'status': 'exists', 'md5': checksum, 'file': existing}
                                panel.finish(slot, state['files'][key])
                                save()
                                continue
                            previous = state['files'].get(key, {})
                            if previous.get('status') in ('uploading', 'uncertain'):
                                panel.finish(slot, {'status': 'uncertain'})
                                failures += 1
                                continue
                            state['files'][key] = {'status': 'uploading', 'size': size, 'md5': checksum}
                            save()  # Before the remote side effect, to prevent blind duplicate uploads.
                            task = pool.submit(transfer, path, destination, size, checksum,
                                               lambda _slot=slot, **fields: panel.update(_slot, **fields))
                            tasks[task] = (slot, key)
                            submitted += 1
                            break
                    for task in as_completed(tasks):
                        slot, key = tasks[task]
                        try:
                            result = task.result()
                            inventory.append(result['file'])
                        except Exception as error:
                            result = {'status': 'uncertain', 'error': error_text(error)}
                            failures += 1
                        state['files'][key].update(result)
                        panel.finish(slot, result)
                        save()
        print(f'Finalizado: {submitted} envios; {failures} casos requieren revision. Estado: {args.state}')
        for path, result in state['files'].items():
            if result.get('error'):
                print(f'{Path(path).name}: {result["error"]}')
        return 1 if failures else 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print('\nDetenido. El estado se conserva; no se borraron archivos locales.')
        raise SystemExit(130)
