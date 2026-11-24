#!/usr/bin/env python3
"""Upload a reviewed Cineari episode plan to GoFile, with resumable local records."""
import argparse
from portable_file_lock import acquire
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time
import uuid
from urllib.parse import urlparse

import requests

from upload_movies_b2 import load_local_env
from build_multisource_catalog import OUTPUT


def safe_name(value):
    return re.sub(r'[\x00-\x1f/\\:"<>|?*]', '_', value).strip()[:150]


class GoFile:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers['Authorization'] = 'Bearer ' + os.environ['GOFILE_TOKEN']

    def api(self, method, path, **kwargs):
        response = self.session.request(method, 'https://api.gofile.io/' + path, timeout=30, **kwargs)
        response.raise_for_status()
        data = response.json()
        if data.get('status') != 'ok':
            raise ValueError('GoFile: ' + str(data.get('status')))
        return data['data']

    def children(self, folder):
        items = []
        page = 1
        while True:
            result = self.api('GET', 'contents/' + folder, params={'page': page, 'pageSize': 100})
            children = result.get('children') or {}
            items.extend(children.values() if isinstance(children, dict) else children)
            metadata = result.get('metadata') or {}
            # A full page may have successors even when this API omits metadata.
            if not children or len(children) < 100 or metadata.get('hasNextPage') is False:
                return items
            page += 1

    def upload(self, media, filename, folder):
        boundary = 'cineari-' + uuid.uuid4().hex
        headers = ''.join(f'{key}: {value}\r\n' for key, value in media.get('headers', {}).items()
                          if '\r' not in value and '\n' not in value)
        command = ['ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error', '-rw_timeout', '30000000']
        if headers:
            command += ['-headers', headers]
        command += ['-i', media['url'], '-map', '0:v:0', '-map', '0:a:0?', '-c', 'copy', '-f', 'matroska', 'pipe:1']
        # Bounded pipe + 1 MiB read chunks; stderr is capped by ffmpeg's error log level.
        with tempfile.TemporaryFile() as errors:
            process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=errors)

            def body():
                yield (f'--{boundary}\r\nContent-Disposition: form-data; name="folderId"\r\n\r\n{folder}\r\n'
                       f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\n'
                       'Content-Type: video/x-matroska\r\n\r\n').encode()
                sent = 0
                started = last = time.monotonic()
                while chunk := process.stdout.read(1024 * 1024):
                    yield chunk
                    sent += len(chunk)
                    if time.monotonic() - last > 10:
                        print(f'  {sent/1048576:.1f} MiB · {sent/1048576/(time.monotonic()-started):.2f} MiB/s', flush=True)
                        last = time.monotonic()
                if process.wait() != 0 or not sent:
                    raise ValueError('No se completó la lectura del video')
                yield f'\r\n--{boundary}--\r\n'.encode()

            try:
                response = self.session.post('https://upload.gofile.io/uploadfile', data=body(),
                    headers={'Content-Type': 'multipart/form-data; boundary=' + boundary}, timeout=(30, 300))
                response.raise_for_status()
                result = response.json()
                if result.get('status') != 'ok':
                    raise ValueError('GoFile upload: ' + str(result.get('status')))
                if process.poll() not in (0, None):
                    raise ValueError('El proceso de video falló')
                return result['data']
            finally:
                if process.poll() is None:
                    process.kill()
                process.wait()
                process.stdout.close()


def resolve(source):
    url = source['embed_url']
    if source.get('provider') == 'va' or urlparse(url).hostname in ('videoapp.zip', 'videoapi.la', 'vimeos.net'):
        raise ValueError('VideoAPI excluido')
    if 'pixeldrain.com/api/file/' in url:
        with requests.get(url, headers={'Range': 'bytes=0-0'}, stream=True, timeout=20) as response:
            response.raise_for_status()
            if 'text/html' in response.headers.get('Content-Type', ''):
                raise ValueError('Pixeldrain devolvió HTML, no video')
        return {'url': url, 'headers': {}}
    import yt_dlp
    with yt_dlp.YoutubeDL({'quiet': True, 'no_warnings': True, 'socket_timeout': 20,
                          'retries': 0, 'format': 'best'}) as downloader:
        data = downloader.extract_info(url, download=False)
        return {'url': data['url'], 'headers': data.get('http_headers') or {}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan', type=Path, default=OUTPUT / 'cineari-gofile-plan.json')
    parser.add_argument('--urls', type=Path, default=OUTPUT / 'cineari-episodes.txt',
                        help='Única lista permitida de URLs; una línea por serie')
    parser.add_argument('--state', type=Path, default=OUTPUT / 'cineari-gofile-state.json')
    parser.add_argument('--limit', type=int, default=0, help='0 = todos los episodios pendientes')
    parser.add_argument('--category', choices=('shows', 'animes'), default='shows')
    args = parser.parse_args()
    if args.limit < 0:
        parser.error('--limit no puede ser negativo')
    allowed_urls = set(args.urls.read_text().split())
    load_local_env()
    if not os.environ.get('GOFILE_TOKEN'):
        parser.error('Falta GOFILE_TOKEN en .env.local')
    args.state.parent.mkdir(parents=True, exist_ok=True)
    lock = args.state.with_suffix('.lock').open('a+b')
    acquire(lock)
    state = json.loads(args.state.read_text()) if args.state.exists() else {'folders': {}, 'episodes': {}}

    def save():
        temporary = args.state.with_suffix('.tmp')
        temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2) + '\n')
        temporary.replace(args.state)

    client = GoFile()
    account = client.api('GET', 'accounts/getid')['id']
    root = client.api('GET', 'accounts/' + account)['rootFolder']
    root_items = client.children(root)
    completed = failures = 0
    for show in json.loads(args.plan.read_text()):
        tid = str(int(show['tmdb_id']))
        folder_name = f'{tid} - {safe_name(show["title"])}'
        folder = state['folders'].get(tid)
        if not folder:
            folder = next((item for item in root_items if item.get('type') == 'folder' and item.get('name') == folder_name), None)
        existing = {item['name']: item for item in client.children(folder['id'])} if folder else {}
        for episode in show['episodes']:
            sources = [source for source in episode['sources']
                       if source['embed_url'] in allowed_urls and source.get('provider') != 'va'
                       and urlparse(source['embed_url']).hostname not in ('videoapp.zip', 'videoapi.la', 'vimeos.net')]
            if not sources:
                continue
            if args.limit and completed >= args.limit:
                return 0
            key = f'{tid}:S{episode["season"]:02d}E{episode["episode"]:02d}'
            filename = f'{tid} - {safe_name(show["title"])} - S{episode["season"]:02d}E{episode["episode"]:02d}.mkv'
            if state['episodes'].get(key, {}).get('status') == 'uploaded':
                continue
            if filename in existing:
                state['episodes'][key] = {'status': 'uploaded', 'file': existing[filename]}
                save()
                continue
            print(f'Preparando {filename}', flush=True)
            success = False
            for source in sources:
                try:
                    media = resolve(source)
                    from media_folders import series_folder
                    destination = series_folder('gofile', client, args.category, tid, show['title'], episode['season'])
                    season_files = {item['name']: item for item in client.children(destination)}
                    if filename in season_files:
                        state['episodes'][key] = {'status': 'uploaded', 'file': season_files[filename]}
                        save()
                        success = True
                        break
                    state['episodes'][key] = {'status': 'uploading', 'name': filename}
                    save()
                    result = client.upload(media, filename, destination)
                    if result.get('parentFolder') != destination or not result.get('size'):
                        raise ValueError('Destino o tamaño de archivo inesperado')
                    state['episodes'][key] = {'status': 'uploaded', 'file': result}
                    existing[filename] = result
                    completed += 1
                    print(f'SUBIDO {filename} ({result["size"]} bytes)', flush=True)
                    success = True
                    save()
                    break
                except Exception as error:
                    # Do not log signed media URLs or authorization headers.
                    print(f'  Fuente {source.get("provider")}: {type(error).__name__}', flush=True)
            if not success:
                failures += 1
                state['episodes'][key] = {'status': 'failed', 'name': filename}
                save()
    print(f'Finalizado: {completed} subidos; {failures} fallidos', flush=True)
    return 1 if failures else 0


if __name__ == '__main__':
    raise SystemExit(main())
