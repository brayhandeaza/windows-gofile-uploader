#!/usr/bin/env python3
"""Build a resumable movie or episode-level TV embed catalog.

Only observed embeds are recorded. A listing or a TMDB match alone never
proves that a source plays, or that its audio is Spanish.
"""

import argparse
import base64
import json
import re
import time
import unicodedata
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from html import unescape
from pathlib import Path
from urllib.parse import urljoin, urlparse, urlsplit, urlunsplit

import requests

from build_vidspark_tv_catalog import tmdb_key


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "output"
HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
           "AppleWebKit/537.36 Chrome/120.0 Safari/537.36"}
SITES = {
    "pelispedia": "https://pelispedia.soy",
    "5movies": "https://www.5movies.cc",
    "cuevana": "https://www2.cuevana3.now",
    "pelisplushd": "https://pelisplushd.la",
    "cineari": "https://cineari.com",
    "locopelis": "https://www.locopelis.com",
    "vidspark": "https://vidspark.to",
}
EMBED_HOSTS = ("streamwish.to", "vidhidepro.com", "vidara.to", "vidsrc.sh",
               "vidsrc.to", "vidsrc.xyz", "filemoon.sx", "streamtape.com",
               "doodstream.com", "dood.pm", "uqload.com", "mixdrop.co", "vidspark.to",
               "zplayer.live", "streamlare.com", "voe.sx", "pixeldrain.com",
               "ok.ru", "videos.cineari.com", "uqload.is", "uqload.vc")


def fetch(url, method="GET", data=None):
    for attempt in range(3):
        try:
            response = requests.request(method, url, data=data, headers=HEADERS, timeout=25)
            response.raise_for_status()
            return response.text
        except requests.RequestException:
            if attempt == 2:
                raise
            time.sleep(1 + attempt * 2)


def links(html, base):
    return [urljoin(base, unescape(value)) for value in
            re.findall(r'\bhref\s*=\s*(?:["\']([^"\']+)["\']|([^\s>]+))', html, re.I)
            for value in [''.join(value)]]


def clean_url(url, base):
    url = urljoin(base, unescape(url).replace("&amp;", "&"))
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return None
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path,
                       parts.query, ""))


def title_year(html):
    tag = re.search(r'<meta[^>]+(?:property|name)=["\']og:title["\'][^>]+content=["\']([^"\']+)', html, re.I)
    if not tag:
        tag = re.search(r'<title[^>]*>(.*?)</title>', html, re.I | re.S)
    title = unescape(re.sub(r'<[^>]+>', '', tag.group(1))).strip() if tag else ''
    title = re.sub(r'^(?:Ver|Watch)\s+', '', title, flags=re.I)
    title = re.split(r'\s+(?:Online|Gratis|HD|Full Movie|Full Serie|\|)\b', title, maxsplit=1, flags=re.I)[0].strip(' -|')
    year = re.search(r'\b(?:19|20)\d{2}\b', title)
    return re.sub(r'\s*\((?:19|20)\d{2}\).*$', '', title).strip(), int(year.group()) if year else None


def normalized(text):
    return re.sub(r'[^a-z0-9]+', '', unicodedata.normalize('NFKD', text or '')
                  .encode('ascii', 'ignore').decode().lower())


def catalog_title(title):
    title = re.sub(r'^(?:Serie|Series|Película|Pelicula|Movie)\s*:\s*', '', title or '', flags=re.I)
    return re.sub(r'\s*(?:[:|\-]\s*)?(?:Serie Completa|Temporadas? y Episodios? Completos?|Online).*$',
                  '', title, flags=re.I).strip()


def resolve_tmdb(kind, title, year, key, cache):
    title = catalog_title(title)
    lookup = f'{kind}:{normalized(title)}:{year or ""}'
    if lookup in cache:
        return cache[lookup]
    if not title:
        return None
    params = {'query': title}
    if year:
        params['year' if kind == 'movie' else 'first_air_date_year'] = year
    response = requests.get(f'https://api.themoviedb.org/3/search/{kind}',
                            params={'api_key': key, 'language': 'es-ES', **params},
                            headers=HEADERS, timeout=(5, 8))
    response.raise_for_status()
    result = response.json()
    matches = [row for row in result.get('results', []) if
               normalized(row.get('title') or row.get('name')) == normalized(title) or
               normalized(row.get('original_title') or row.get('original_name')) == normalized(title)]
    if year:
        matches = [row for row in matches if not (row.get('release_date') or row.get('first_air_date')) or
                   (row.get('release_date') or row.get('first_air_date')).startswith(str(year))]
    value = int(matches[0]['id']) if len(matches) == 1 else None
    cache[lookup] = value
    return value


def match_key(kind, title, year):
    return f'{kind}:{normalized(catalog_title(title))}:{year or ""}'


def cached_tmdb_id(kind, item, matches):
    return item.get('tmdb_id') or matches.get(match_key(kind, item.get('title'), item.get('year')))


def source(site, provider, url, page, season=None, episode=None, language=None):
    cleaned = clean_url(url, page)
    if not cleaned:
        return None
    result = {'site': site, 'provider': provider,
              'kind': 'direct' if urlsplit(cleaned).path.lower().endswith(('.mp4', '.m3u8')) else 'embed',
              'embed_url': cleaned,
              'source_pages': [page], 'availability': 'candidate',
              'audio_languages': [], 'spanish_audio_verified': False}
    if language:
        result['declared_language'] = language
    if season is not None and episode is not None:
        result['season_number'], result['episode_number'] = season, episode
    return result


def embeds(html, site, page, season=None, episode=None):
    candidates = re.findall(r'\b(?:data-url|data-src|src|href)\s*=\s*["\']([^"\']+)', html, re.I)
    found = []
    for value in candidates:
        url = clean_url(value, page)
        if not url:
            continue
        host = urlparse(url).hostname or ''
        if not any(host == allowed or host.endswith('.' + allowed) for allowed in EMBED_HOSTS) and not (site == 'locopelis' and
                                             host == 'www.locopelis.com' and '/player/' in url):
            continue
        item = source(site, host, url, page, season, episode)
        if item:
            found.append(item)
    return found


def listing(site, kind, page):
    base = SITES[site]
    if site == 'pelispedia':
        path = 'pelicula' if kind == 'movie' else 'serie'
        url = f'{base}/{path}/' + (f'page/{page}/' if page > 1 else '')
        pattern = rf'/{path}/[^/?#]+/?$'
    elif site == 'pelisplushd':
        path = 'peliculas' if kind == 'movie' else 'series'
        url = f'{base}/{path}' + (f'?page={page}' if page > 1 else '')
        pattern = r'/pelicula/[^/?#]+/?$' if kind == 'movie' else r'/serie/[^/?#]+/?$'
    elif site == 'locopelis':
        if kind != 'movie':
            return [], False
        url = base + (f'/page/{page}/' if page > 1 else '/')
        pattern = r'/pelicula/\d+/[^/?#]+\.html$'
    elif site == '5movies':
        url = f'{base}/home' + (f'?page={page}' if page > 1 else '')
        pattern = r'/[a-z0-9-]+-(?:19|20)\d\d$'
    elif kind == 'tv' and site == 'cuevana':
        url = f'{base}/serie' + (f'/page/{page}' if page > 1 else '')
        pattern = r'/serie/(?!page/)\S+/?$'
    elif kind == 'tv' and site == 'cineari':
        url = f'{base}/ver-series/' + (f'page/{page}/' if page > 1 else '')
        pattern = r'/serie/[^/?#]+/?$'
    else:
        return [], False
    try:
        html = fetch(url)
    except requests.HTTPError as error:
        if error.response is not None and error.response.status_code == 404:
            return [], False
        raise
    matches = [link for link in links(html, url) if
               urlparse(link).hostname == urlparse(base).hostname and
               re.fullmatch(pattern, urlparse(link).path)]
    return list(dict.fromkeys(matches)), bool(matches)


def episode_links(html, page_url, site):
    patterns = {
        'cuevana': r'/episodio/[^/?#]+-(\d+)x(\d+)/?$',
        'pelispedia': r'/temporada-(\d+)/episodio-(\d+)/?$',
    }
    pattern = patterns[site]
    found = {}
    for url in links(html, page_url):
        match = re.search(pattern, urlparse(url).path, re.I)
        if match and urlparse(url).hostname == urlparse(page_url).hostname:
            found[url] = (int(match.group(1)), int(match.group(2)))
    return found


def fetch_episode_sources(site, page_url, episodes):
    def inspect(entry):
        url, (season, episode) = entry
        return embeds(fetch(url), site, url, season, episode)
    players = []
    with ThreadPoolExecutor(max_workers=6) as pool:
        for result in pool.map(inspect, episodes.items()):
            players.extend(result)
    return players


def cineari_episode_sources(html, page_url):
    players = []
    for raw in re.findall(r'\bdata-series=\'([^\']+)\'', html, re.I | re.S):
        try:
            episodes = json.loads(unescape(raw))
        except ValueError:
            continue
        for episode in episodes:
            season, number = episode.get('s'), episode.get('e')
            if not isinstance(season, int) or not isinstance(number, int):
                continue
            for field, encrypted in episode.items():
                if field in ('s', 'e', 'title') or not encrypted:
                    continue
                try:
                    decoded = base64.b64decode(encrypted[::-1]).decode('utf-8')
                    url = decoded.translate(str.maketrans(
                        'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz',
                        'NOPQRSTUVWXYZABCDEFGHIJKLMnopqrstuvwxyzabcdefghijklm'))
                except (ValueError, UnicodeError):
                    continue
                player = source('cineari', field, url, page_url, season, number)
                if player:
                    players.append(player)
    return players


def inspect_page(site, kind, page_url):
    html = fetch(page_url)
    title, year = title_year(html)
    if not year:
        year_match = re.search(r'-(19\d{2}|20\d{2})/?$', urlparse(page_url).path)
        year = int(year_match.group(1)) if year_match else None
    if site == '5movies' and (('Full Serie' in html[:6000]) != (kind == 'tv')):
        return {'site': site, 'page_url': page_url, 'title': title, 'year': year,
                'tmdb_id': None, 'sources': []}
    match = re.search(r'\bdata-tmdb=["\'](\d+)', html, re.I) if site == 'pelispedia' else None
    tmdb_id = int(match.group(1)) if match else None
    if site == '5movies':
        match = re.search(r'\bid=(?:["\']?)play-now(?:["\']?)[^>]*\bdata-id=["\'](\d+)', html, re.I)
        if not match:
            return {'site': site, 'page_url': page_url, 'title': title, 'year': year,
                    'tmdb_id': None, 'sources': []}
        watch = f'{SITES[site]}/watch?id={match.group(1)}'
        watch_html = fetch(watch)
        tmdb = re.search(r'\bdata-tid=["\'](\d+)', watch_html)
        tmdb_id = int(tmdb.group(1)) if tmdb else None
        providers = set(re.findall(r'class=["\'][^"\']*server-links[^"\']*["\'][^>]*\bdata-type=["\']([^"\']+)', watch_html))
        players = []
        for provider in sorted(providers):
            if not re.fullmatch(r'[A-Za-z0-9_-]{2,30}', provider):
                continue
            try:
                response = fetch(f'{SITES[site]}/ajax_movie', 'POST',
                                 {'id': match.group(1), 'types': provider})
            except requests.RequestException:
                continue
            candidates = re.findall(r'https?://[^\s"\'<>\\]+', response)
            players.extend(filter(None, (source(site, provider, url, watch) for url in candidates)))
        return {'site': site, 'page_url': page_url, 'title': title, 'year': year,
                'tmdb_id': tmdb_id, 'sources': players}
    if kind == 'tv' and site in ('cuevana', 'pelispedia'):
        players = fetch_episode_sources(site, page_url, episode_links(html, page_url, site))
    elif kind == 'tv' and site == 'cineari':
        players = cineari_episode_sources(html, page_url)
    elif site == 'pelisplushd' and kind == 'tv':
        chapter_urls = [url for url in links(html, page_url) if
                         re.search(r'/temporada/\d+/capitulo/\d+/?$', url)]
        players = []
        for episode_url in dict.fromkeys(chapter_urls):
            match = re.search(r'/temporada/(\d+)/capitulo/(\d+)', episode_url)
            try:
                players.extend(embeds(fetch(episode_url), site, episode_url,
                                      int(match.group(1)), int(match.group(2))))
            except requests.RequestException:
                continue
    else:
        players = embeds(html, site, page_url)
    return {'site': site, 'page_url': page_url, 'title': title, 'year': year,
            'tmdb_id': tmdb_id, 'sources': players}


def load_cache(path):
    rows = {}
    if path.exists():
        for line in path.read_text().splitlines():
            try:
                row = json.loads(line)
                rows[(row['site'], row['page_url'])] = row
            except (ValueError, KeyError):
                continue
    return rows


def save_json(path, data):
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(data, ensure_ascii=False, separators=(',', ':')))
    temporary.replace(path)


def infer_resume_page(site, kind, cache, matches):
    """Recover the completed listing prefix left by older runs without checkpoints."""
    if not any(cached_site == site for cached_site, _ in cache):
        return 1
    first_urls = None
    checked = {}

    def complete(page):
        nonlocal first_urls
        if page in checked:
            return checked[page]
        urls, _ = listing(site, kind, page)
        if page == 1:
            first_urls = urls
        if not urls or (page > 1 and first_urls is not None and urls == first_urls):
            checked[page] = (False, True)
        else:
            checked[page] = (all(
                (site, url) in cache and
                (cached_tmdb_id(kind, cache[(site, url)], matches) is not None or
                 match_key(kind, cache[(site, url)].get('title'), cache[(site, url)].get('year')) in matches)
                for url in urls), False)
        return checked[page]

    if not complete(1)[0]:
        return 1
    low, high = 1, 2
    while high <= 16384 and complete(high)[0]:
        low, high = high, high * 2
    if high > 16384:
        return low + 1
    while low + 1 < high:
        middle = (low + high) // 2
        if complete(middle)[0]:
            low = middle
        else:
            high = middle
    return None if complete(high)[1] else high


def add(rows, kind, tmdb_id, title, item):
    if not tmdb_id:
        return 0
    added = 0
    row = rows.setdefault(tmdb_id, {'tmdb_id': tmdb_id, 'type': kind,
                                    'title': title, 'source_pages': [],
                                    'sources': [] if kind == 'movie' else None,
                                    'seasons': [] if kind == 'tv' else None})
    if item['page_url'] not in row['source_pages']:
        row['source_pages'].append(item['page_url'])
    for candidate in item.get('sources') or []:
        if kind == 'tv':
            season_no = candidate.get('season_number')
            episode_no = candidate.get('episode_number')
            if season_no is None or episode_no is None:
                continue
            season = next((s for s in row['seasons'] if s['season_number'] == season_no), None)
            if season is None:
                season = {'season_number': season_no, 'episodes': []}
                row['seasons'].append(season)
            episode = next((e for e in season['episodes'] if e['episode_number'] == episode_no), None)
            if episode is None:
                episode = {'episode_number': episode_no, 'sources': []}
                season['episodes'].append(episode)
            destination = episode['sources']
        else:
            destination = row['sources']
        previous = next((s for s in destination if s['embed_url'] == candidate['embed_url']), None)
        if previous:
            previous['source_pages'] = list(dict.fromkeys(previous['source_pages'] + candidate['source_pages']))
            previous['sites'] = list(dict.fromkeys(previous.get('sites', [previous['site']]) + [candidate['site']]))
        else:
            destination.append(candidate)
            added += 1
    return added


class Progress:
    """One changing status line, with readable updates when output is redirected."""

    def __init__(self):
        self.last = 0
        self.is_tty = sys.stdout.isatty()

    def show(self, stage, rows, sources, scanned=0, pending=0, errors=0, force=False):
        now = time.monotonic()
        if not force and now - self.last < (0.25 if self.is_tty else 5):
            return
        message = (f'{stage} · páginas revisadas: {scanned} · pendientes: {pending} · '
                   f'títulos: {len(rows)} · fuentes: {sources} · errores: {errors}')
        print(('\r\033[K' if self.is_tty else '') + message,
              end='' if self.is_tty else '\n', flush=True)
        self.last = now

    def end(self):
        if self.is_tty:
            print(flush=True)


def associate_items(items, kind, key, matches, workers, rows, errors, unresolved,
                    progress, stage, scanned, source_count):
    """Resolve only missing TMDB matches concurrently, then merge on the main thread."""
    items = list(items)
    missing = {}
    for item in items:
        if item.get('tmdb_id') or not item.get('title'):
            continue
        lookup = match_key(kind, item['title'], item.get('year'))
        if lookup not in matches:
            missing.setdefault(lookup, item)
    if missing:
        with ThreadPoolExecutor(max_workers=min(workers, 8)) as pool:
            futures = {pool.submit(resolve_tmdb, kind, item['title'], item.get('year'), key, {}):
                       (lookup, item) for lookup, item in missing.items()}
            remaining = len(futures)
            for future in as_completed(futures):
                lookup, item = futures[future]
                try:
                    matches[lookup] = future.result()
                except (OSError, ValueError, requests.RequestException) as error:
                    errors.append({'site': item['site'], 'page_url': item['page_url'],
                                   'stage': 'tmdb', 'error': type(error).__name__})
                remaining -= 1
                progress.show(stage, rows, source_count, scanned, remaining, len(errors))
    for item in items:
        tmdb_id = cached_tmdb_id(kind, item, matches)
        lookup = match_key(kind, item.get('title'), item.get('year'))
        if tmdb_id:
            source_count += add(rows, kind, tmdb_id, item.get('title'), item)
        elif lookup in matches:
            unresolved.append({'site': item['site'], 'page_url': item['page_url'],
                               'title': item.get('title')})
        progress.show(stage, rows, source_count, scanned, 0, len(errors))
    return source_count


def existing_rows(kind):
    if kind != 'movie':
        path = OUTPUT / 'vidspark_tv_catalog.json'
        if path.exists():
            for show in json.loads(path.read_text()).get('shows', []):
                players = []
                for season in show.get('seasons', []):
                    for episode in season.get('episodes', []):
                        for server in episode.get('servers', []):
                            url = server.get('embed_url')
                            if not url:
                                continue
                            site = server.get('provider', 'vidspark')
                            item = source(site, site, url, url, season['season_number'], episode['episode_number'])
                            if item:
                                item.update({k: server[k] for k in ('availability', 'audio_languages',
                                             'spanish_audio_verified', 'checked_at') if k in server})
                                players.append(item)
                yield {'site': 'vidspark', 'page_url': f'https://vidspark.to/tv/{show["tmdb_id"]}',
                       'title': show.get('title'), 'tmdb_id': show['tmdb_id'], 'sources': players}
        return
    page_ids = {}
    for site, name in [('cineari', 'cineari-movies.tmdb.json'),
                       ('cuevana', 'cubana-movies.tmdb.json')]:
        path = OUTPUT / name
        if not path.exists():
            continue
        data = json.loads(path.read_text())
        for movie in data.get('movies', []):
            tmdb_id = movie.get('tmdb_id')
            page = movie.get('source_page_url') or movie.get('page_url')
            if not tmdb_id or not page:
                continue
            if site == 'cuevana':
                page_ids[page.rstrip('/')] = int(tmdb_id)
            players = []
            for value in (movie.get('player_embeds') or []) + (movie.get('streams') or []):
                url = value if isinstance(value, str) else value.get('url') or value.get('embed_url')
                if url:
                    player = source(site, urlparse(url).hostname or site, url, page)
                    if player:
                        players.append(player)
            yield {'site': site, 'page_url': page, 'title': movie.get('title'),
                   'tmdb_id': int(tmdb_id), 'sources': players}
    path = OUTPUT / 'cuevana_latino_embeds.jsonl'
    if path.exists():
        for line in path.read_text().splitlines():
            try:
                item = json.loads(line)
            except ValueError:
                continue
            player = source('cuevana', item.get('provider', 'embed'),
                            item.get('embed_url'), item.get('page_url'),
                            language=item.get('language')) if item.get('embed_url') else None
            if player:
                yield {'site': 'cuevana', 'page_url': item['page_url'],
                       'title': item.get('title'), 'tmdb_id': item.get('tmdb_id') or
                       page_ids.get(item['page_url'].rstrip('/')),
                       'sources': [player]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--type', choices=('movie', 'tv'), required=True)
    parser.add_argument('--workers', type=int, default=8)
    parser.add_argument('--max-pages', type=int, default=0, help='0 scans all available listing pages')
    parser.add_argument('--sites', nargs='+', choices=tuple(SITES), default=list(SITES))
    parser.add_argument('--refresh', action='store_true', help='Reinspect previously cached detail pages')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    if not 1 <= args.workers <= 24 or args.max_pages < 0:
        parser.error('workers must be 1–24 and max-pages >= 0')
    output = args.output or OUTPUT / f'satoru-{args.type}-sources.json'
    cache_path = OUTPUT / f'multisource-{args.type}-pages.jsonl'
    match_path = OUTPUT / f'multisource-{args.type}-tmdb-matches.json'
    state_path = OUTPUT / f'multisource-{args.type}-checkpoint.json'
    OUTPUT.mkdir(parents=True, exist_ok=True)
    progress = Progress()
    progress.show('Cargando progreso guardado', {}, 0, force=True)
    cache = load_cache(cache_path)
    matches = json.loads(match_path.read_text()) if match_path.exists() else {}
    state = json.loads(state_path.read_text()) if state_path.exists() else {'sites': {}}
    key = tmdb_key()
    unresolved = []
    errors = []
    rows = {}
    source_count = 0
    scanned = 0
    progress.show('Uniendo catálogos locales', rows, source_count, force=True)
    for item in existing_rows(args.type):
        if item['site'] in args.sites:
            source_count += add(rows, args.type, item.get('tmdb_id'), item.get('title'), item)
            progress.show('Uniendo catálogos locales', rows, source_count)
    progress.show(f'Recuperando {len(cache)} páginas de detalle guardadas', rows, source_count, force=True)
    source_count = associate_items(
        (item for item in cache.values() if item['site'] in args.sites),
        args.type, key, matches, args.workers, rows, errors, unresolved,
        progress, 'Recuperando progreso guardado', scanned, source_count)
    save_json(match_path, matches)
    for site in args.sites:
        if site == 'vidspark' or (site == 'locopelis' and args.type == 'tv') or (args.type == 'movie' and site in ('cuevana', 'cineari')):
            continue
        checkpoint = state['sites'].get(site)
        if args.refresh:
            checkpoint = None
        if checkpoint is None:
            progress.show(f'{site}: localizando primera página pendiente', rows, source_count, force=True)
            try:
                inferred = infer_resume_page(site, args.type, cache, matches)
            except requests.RequestException as error:
                errors.append({'site': site, 'resume': True, 'error': str(error)})
                inferred = 1
            checkpoint = {'next_page': inferred or 1, 'done': inferred is None}
            state['sites'][site] = checkpoint
            save_json(state_path, state)
        if checkpoint.get('done') and site == 'cuevana' and args.type == 'tv':
            # An empty listing page is not necessarily the end of Cuevana's catalog.
            next_page = int(checkpoint['next_page']) + 1
            try:
                following, _ = listing(site, args.type, next_page)
                if any((site, url) not in cache for url in following):
                    checkpoint.update({'next_page': next_page, 'done': False})
                    save_json(state_path, state)
            except requests.RequestException as error:
                errors.append({'site': site, 'listing_page': next_page, 'error': str(error)})
        if checkpoint.get('done'):
            progress.show(f'{site}: ya completado', rows, source_count, scanned, errors=len(errors), force=True)
            continue
        page = int(checkpoint['next_page'])
        seen_urls = set()
        while not args.max_pages or page <= args.max_pages:
            progress.show(f'{site}: leyendo página {page}', rows, source_count,
                          scanned, errors=len(errors), force=page == 1)
            try:
                urls, has_results = listing(site, args.type, page)
            except requests.RequestException as error:
                errors.append({'site': site, 'listing_page': page, 'error': str(error)})
                break
            urls = [url for url in urls if url not in seen_urls]
            pending = [url for url in urls if args.refresh or (site, url) not in cache]
            progress.show(f'{site}: inspeccionando página {page}', rows, source_count,
                          scanned, len(pending), len(errors))
            with ThreadPoolExecutor(max_workers=args.workers) as pool:
                futures = {pool.submit(inspect_page, site, args.type, url): url for url in pending}
                for future in as_completed(futures):
                    try:
                        item = future.result()
                        cache[(site, item['page_url'])] = item
                        with cache_path.open('a') as file:
                            file.write(json.dumps(item, ensure_ascii=False) + '\n')
                    except requests.RequestException as error:
                        errors.append({'site': site, 'page_url': futures[future], 'error': str(error)})
                    progress.show(f'{site}: inspeccionando página {page}', rows, source_count,
                                  scanned, len(futures) - 1, len(errors))
                    futures.pop(future)
            source_count = associate_items(
                (cache[(site, url)] for url in urls if (site, url) in cache),
                args.type, key, matches, args.workers, rows, errors, unresolved,
                progress, f'{site}: asociando TMDB página {page}', scanned, source_count)
            save_json(match_path, matches)
            scanned += 1
            progress.show(f'{site}: página {page} completada', rows, source_count,
                          scanned, 0, len(errors))
            if not has_results or not urls:
                if site == 'cuevana' and args.type == 'tv':
                    try:
                        following, _ = listing(site, args.type, page + 1)
                        if any(url not in seen_urls for url in following):
                            page += 1
                            checkpoint['next_page'] = page
                            save_json(state_path, state)
                            continue
                    except requests.RequestException as error:
                        errors.append({'site': site, 'listing_page': page + 1, 'error': str(error)})
                checkpoint.update({'next_page': page, 'done': True})
                save_json(state_path, state)
                break
            seen_urls.update(urls)
            page += 1
            checkpoint['next_page'] = page
            save_json(state_path, state)
    progress.show('Escribiendo catálogo', rows, source_count, scanned, errors=len(errors), force=True)
    ordered = [rows[id] for id in sorted(rows)]
    for row in ordered:
        if args.type == 'tv':
            row['seasons'].sort(key=lambda s: s['season_number'])
            for season in row['seasons']:
                season['episodes'].sort(key=lambda e: e['episode_number'])
    result = {'schema_version': 1, 'type': args.type,
              'generated_at': datetime.now(timezone.utc).isoformat(),
              'sites': args.sites, 'title_count': len(ordered),
              'source_count': sum(len(row['sources'] or []) if args.type == 'movie' else
                                  sum(len(e['sources']) for s in row['seasons'] for e in s['episodes'])
                                  for row in ordered),
              'unresolved': unresolved, 'errors': errors,
              'audio_policy': 'Embeds are candidates; Spanish audio is not verified.',
              'titles': ordered}
    temp = output.with_suffix(output.suffix + '.tmp')
    temp.write_text(json.dumps(result, ensure_ascii=False, separators=(',', ':')))
    temp.replace(output)
    progress.end()
    print(f'Guardado {output}: {result["title_count"]} títulos, {result["source_count"]} fuentes')


if __name__ == '__main__':
    main()
