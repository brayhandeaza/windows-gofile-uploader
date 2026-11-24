"""Read a GoFile inventory and distinguish movies from individual episodes."""
from pathlib import PurePosixPath
import re

VIDEO_EXTENSIONS = {'.mp4', '.mkv', '.avi', '.mov', '.m4v', '.webm', '.ts', '.m2ts'}


def identity(name):
    match = re.match(r'^(?:TMDB[:=_\s-]*)?(\d+)(?=\s|[-_:])', name, re.I)
    if not match:
        return None
    episode = re.search(r'\bS(\d+)[ ._-]*E(\d+)\b|\b(\d{1,2})x(\d{1,4})\b', name, re.I)
    if episode:
        season, number = episode.group(1, 2) if episode.group(1) else episode.group(3, 4)
        return int(match.group(1)), int(season), int(number)
    return int(match.group(1)), None, None


def collect_inventory(client, folder):
    pending, visited, files = [folder], set(), []
    while pending:
        current = pending.pop()
        if current in visited:
            continue
        visited.add(current)
        for item in client.children(current):
            if item.get('type') == 'folder':
                pending.append(item['id'])
            elif item.get('type') == 'file':
                row = {key: item.get(key) for key in ('id', 'name', 'size', 'md5', 'parentFolder', 'mimetype')}
                name = item.get('name', '')
                media = PurePosixPath(name).suffix.lower() in VIDEO_EXTENSIONS or str(item.get('mimetype', '')).startswith('video/')
                key = identity(name) if media else None
                row.update(tmdb_id=key[0] if key else None, season=key[1] if key else None,
                           episode=key[2] if key else None, media_kind=('episode' if key[1] is not None else 'movie') if key else None)
                files.append(row)
    return files


def find_existing(client, folder, tmdb_id, season=None, episode=None):
    for item in collect_inventory(client, folder):
        if (item['tmdb_id'], item['season'], item['episode']) == (int(tmdb_id), season, episode):
            return item
    return None
