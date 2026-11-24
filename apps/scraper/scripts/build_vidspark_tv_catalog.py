#!/usr/bin/env python3
"""Build a resumable, episode-level TV catalog from VidSpark and TMDB.

Discovery only proves a show is listed. It does not prove an episode exists or
that its audio is Spanish. Candidate servers keep those facts explicit.
"""

import argparse
import gzip
import json
import os
import re
import ssl
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from html import unescape
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[3]
OUTPUT = ROOT / "apps/scraper/output/vidspark_tv_catalog.json"
ARCHIVE = ROOT / "apps/scraper/catalog/vidspark-tv-catalog.json.gz"
CACHE = ROOT / "apps/scraper/output/vidspark_tv_cache"
AUDIO_CACHE = ROOT / "apps/scraper/output/vidspark_tv_audio_cache"
EXISTING = ROOT / "apps/scraper/output/videoapi_tvshow.json"
VIDSPARK = "https://vidspark.to/api/discover/tv"
TMDB = "https://api.themoviedb.org/3"
AGENT = "Mozilla/5.0 (compatible; SatoruCatalog/1.0)"


def ssl_context():
    """Python.org builds on macOS may not inherit the system trust store."""
    try:
        import certifi
    except ImportError:
        return ssl.create_default_context()
    return ssl.create_default_context(cafile=certifi.where())


SSL_CONTEXT = ssl_context()


def get_json(url, retries=4):
    for attempt in range(retries):
        try:
            with urlopen(Request(url, headers={"User-Agent": AGENT, "Accept": "application/json"}),
                         timeout=25, context=SSL_CONTEXT) as response:
                return json.load(response)
        except HTTPError as error:
            if error.code == 404:
                return None
            if error.code not in (429, 500, 502, 503, 504) or attempt == retries - 1:
                raise
        except (URLError, TimeoutError, json.JSONDecodeError):
            if attempt == retries - 1:
                raise
        time.sleep(min(8, 1.5 * (attempt + 1)))


def get_text(url, referer=None):
    headers = {"User-Agent": AGENT}
    if referer:
        headers["Referer"] = referer
    with urlopen(Request(url, headers=headers), timeout=15, context=SSL_CONTEXT) as response:
        if response.status != 200:
            raise RuntimeError(f"HTTP {response.status}")
        return response.read(2_000_001).decode("utf-8", "replace")


def tmdb_key():
    key = os.environ.get("TMDB_API_KEY")
    if key:
        return key
    settings = ROOT / "apps/android/local.properties"
    match = re.search(r"^tmdb\.apiKey=(.+)$", settings.read_text(), re.MULTILINE) if settings.exists() else None
    if not match:
        raise SystemExit("Configura TMDB_API_KEY o tmdb.apiKey en apps/android/local.properties")
    return match.group(1).strip()


def tmdb_get(path, key, **params):
    query = urlencode({"api_key": key, "language": "es-ES", **params})
    return get_json(f"{TMDB}/{path}?{query}")


def image(path, size="original"):
    return f"https://image.tmdb.org/t/p/{size}{path}" if path else None


def fetch_discovery(workers, limit):
    first = get_json(f"{VIDSPARK}?page=1&resultsPerPage=100")
    if not first or not first.get("result") or first.get("current_page") != 1:
        raise RuntimeError("VidSpark no devolvió la primera página")
    last = first["last_page"]
    pages = {1: first}

    def fetch(page):
        data = get_json(f"{VIDSPARK}?page={page}&resultsPerPage=100")
        if not data or not data.get("result") or data.get("current_page") != page:
            raise RuntimeError(f"Página {page} inválida")
        return page, data

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for future in as_completed(pool.submit(fetch, page) for page in range(2, last + 1)):
            page, data = future.result()
            pages[page] = data
            print(f"\rVidSpark: {len(pages)}/{last} páginas", end="", flush=True)
    print()
    rows = [row for page in range(1, last + 1) for row in pages[page]["data"]]
    if len(rows) != first["total"]:
        raise RuntimeError(f"Catálogo incompleto: {len(rows)} de {first['total']}")
    by_id = {}
    for row in rows:
        try:
            show_id = int(row["tmdbid"])
        except (ValueError, TypeError, KeyError):
            continue
        by_id[show_id] = row
    return list(by_id.items())[:limit] if limit else list(by_id.items()), len(rows)


def load_existing():
    if not EXISTING.exists():
        return {}
    return {int(show["tmdb_id"]): show for show in json.loads(EXISTING.read_text())["shows"]}


def fetch_show(show_id, key):
    path = CACHE / f"{show_id}.json"
    if path.exists():
        return json.loads(path.read_text())
    details = tmdb_get(f"tv/{show_id}", key, append_to_response="images,credits",
                       include_image_language="es,en,null")
    if details is None:
        return None
    seasons = []
    for summary in details.get("seasons", []):
        number = summary["season_number"]
        detail = tmdb_get(f"tv/{show_id}/season/{number}", key)
        if detail is None:
            continue
        seasons.append({
            "season_number": number, "tmdb_id": detail.get("id"), "name": detail.get("name"),
            "synopsis": detail.get("overview"), "air_date": detail.get("air_date"),
            "poster_url": image(detail.get("poster_path"), "w500"),
            "episodes": [{
                "episode_number": episode["episode_number"], "tmdb_id": episode.get("id"),
                "title": episode.get("name"), "synopsis": episode.get("overview"),
                "air_date": episode.get("air_date"), "runtime_minutes": episode.get("runtime"),
                "still_url": image(episode.get("still_path"), "w780"),
                "rating": episode.get("vote_average"), "vote_count": episode.get("vote_count"),
            } for episode in detail.get("episodes", [])],
        })
    pictures = details.get("images") or {}
    backdrops = [image(row.get("file_path")) for row in pictures.get("backdrops", []) if row.get("file_path")]
    if details.get("backdrop_path"):
        backdrops.insert(0, image(details["backdrop_path"]))
    logos = sorted((row for row in pictures.get("logos", []) if row.get("file_path")),
                   key=lambda row: ((row.get("iso_639_1") == "es"), row.get("vote_average") or 0), reverse=True)
    show = {
        "tmdb_id": show_id, "title": details.get("name"), "original_title": details.get("original_name"),
        "synopsis": details.get("overview"), "first_air_date": details.get("first_air_date"),
        "last_air_date": details.get("last_air_date"), "status": details.get("status"),
        "type": details.get("type"), "original_language": details.get("original_language"),
        "origin_countries": details.get("origin_country") or [],
        "genres": [genre["name"] for genre in details.get("genres", [])],
        "episode_runtime_minutes": details.get("episode_run_time") or [],
        "rating": details.get("vote_average"), "vote_count": details.get("vote_count"),
        "artwork": {"poster_url": image(details.get("poster_path"), "w500"),
                    "backdrop_urls": list(dict.fromkeys(backdrops)),
                    "title_logo_url": image(logos[0]["file_path"]) if logos else None},
        "seasons": seasons,
    }
    CACHE.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(show, ensure_ascii=False, separators=(",", ":")))
    temporary.replace(path)
    return show


def server(provider, url, available=None):
    return {"provider": provider, "kind": "embed", "embed_url": url,
            "availability": available or "candidate", "audio_languages": [],
            "spanish_audio_verified": False}


def merge_show(show_id, discovery, metadata):
    if metadata is None:
        metadata = {"tmdb_id": show_id, "title": discovery.get("orig_title"), "seasons": [],
                    "artwork": {"poster_url": None, "backdrop_urls": [], "title_logo_url": None}}
        metadata_status = "tmdb_unavailable"
    else:
        metadata_status = "complete"
    show = {key: value for key, value in metadata.items() if key not in
            ("season_count_videoapi", "episode_count_videoapi", "season_count_tmdb", "episode_count_tmdb")}
    show.update({"tmdb_id": show_id, "imdb_id": discovery.get("imdb_id"),
                 "discovery_source": "vidspark", "discovery_last_update": discovery.get("last_update"),
                 "metadata_status": metadata_status})
    seasons = []
    for season in metadata.get("seasons") or []:
        number = season["season_number"]
        episodes = []
        for episode in season.get("episodes") or []:
            index = episode["episode_number"]
            row = {key: value for key, value in episode.items() if key not in ("videoapi", "servers")}
            row["servers"] = [server("vidspark", f"https://vidspark.to/tv/{show_id}/{number}/{index}")]
            if episode.get("videoapi", {}).get("available"):
                row["servers"].append(server("videoapi", f"https://videoapp.zip/e/tv/{show_id}/{number}/{index}", "listed"))
            row["playable_servers"] = []  # Only Spanish-audio-verified servers belong here.
            episodes.append(row)
        seasons.append({**{key: value for key, value in season.items() if key not in ("episodes", "episode_count_videoapi", "episode_count_tmdb")},
                        "episodes": episodes, "episode_count": len(episodes)})
    show["seasons"] = seasons
    show["season_count"] = len(seasons)
    show["episode_count"] = sum(len(season["episodes"]) for season in seasons)
    return show


def verify_spanish_audio(show_id, season, episode, refresh=False):
    path = AUDIO_CACHE / f"{show_id}_{season}x{episode}.json"
    if path.exists() and not refresh:
        return json.loads(path.read_text())
    embed = f"https://videoapp.zip/e/tv/{show_id}/{season}/{episode}"
    try:
        page = get_text(embed)
        frame_match = re.search(r'<iframe\b[^>]*\bsrc=["\']([^"\']+)', page, re.IGNORECASE)
        if not frame_match:
            raise RuntimeError("no iframe")
        frame = urljoin(embed, unescape(frame_match.group(1)))
        if not frame.startswith("https://vimeos.net/"):
            raise RuntimeError("unexpected iframe origin")
        html = get_text(frame, embed)
        marker = "window.VIDAPI_CF_STREAM_RESPONSE = "
        line = next((line.strip() for line in html.splitlines() if line.strip().startswith(marker)), None)
        if not line:
            raise RuntimeError("no stream manifest")
        streams = json.loads(line[len(marker):].rstrip("; ")).get("data", {}).get("stream_urls") or []
        manifest_url = next((url for url in streams if ".m3u8" in url), None)
        if not manifest_url:
            raise RuntimeError("no HLS manifest")
        manifest = get_text(manifest_url, "https://vimeos.net/")
        tracks = [line for line in manifest.splitlines() if line.startswith("#EXT-X-MEDIA:TYPE=AUDIO")]
        languages = sorted(set(re.findall(r'LANGUAGE="([^"]+)"', "\n".join(tracks))))
        confirmed = any(code.lower() == "es" or code.lower().startswith("es-") for code in languages)
        result = {"spanish_audio_verified": confirmed, "audio_languages": languages,
                  "checked_at": datetime.now(timezone.utc).isoformat()}
        AUDIO_CACHE.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(result, separators=(",", ":")))
        return result
    except (OSError, ValueError, RuntimeError, StopIteration) as error:
        return {"spanish_audio_verified": False, "audio_languages": [], "error": type(error).__name__}


def apply_audio_verification(shows, workers, limit, refresh):
    pending = []
    for show in shows:
        for season in show["seasons"]:
            for episode in season["episodes"]:
                if any(row["provider"] == "videoapi" for row in episode["servers"]):
                    pending.append((show, season, episode))
    selected = pending[:limit] if limit > 0 else pending

    def check(item):
        show, season, episode = item
        return item, verify_spanish_audio(show["tmdb_id"], season["season_number"], episode["episode_number"], refresh)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for number, future in enumerate(as_completed(pool.submit(check, item) for item in selected), 1):
            (_, _, episode), result = future.result()
            videoapi = next(row for row in episode["servers"] if row["provider"] == "videoapi")
            videoapi.update({key: value for key, value in result.items() if key != "error"})
            if result["spanish_audio_verified"]:
                videoapi["availability"] = "audio_verified"
                episode["playable_servers"].append("videoapi")
            print(f"\rAudio español: {number}/{len(selected)} comprobados", end="", flush=True)
    if selected:
        print()
    return len(pending), len(selected)


def preserve_servers(shows, output_path):
    """Retain later StreamWish/StreamTape injections and previous audio checks."""
    if not output_path.exists():
        return
    previous = json.loads(output_path.read_text())
    old_shows = {show["tmdb_id"]: show for show in previous.get("shows", [])}
    for show in shows:
        old = old_shows.get(show["tmdb_id"])
        if not old:
            continue
        old_episodes = {(season["season_number"], episode["episode_number"]): episode
                        for season in old.get("seasons", []) for episode in season.get("episodes", [])}
        for season in show["seasons"]:
            for episode in season["episodes"]:
                old_episode = old_episodes.get((season["season_number"], episode["episode_number"]))
                if not old_episode:
                    continue
                current = {(server["provider"], server["embed_url"]): server for server in episode["servers"]}
                for server in old_episode.get("servers", []):
                    key = (server.get("provider"), server.get("embed_url"))
                    if key in current:
                        current[key].update({field: server[field] for field in
                                             ("availability", "audio_languages", "spanish_audio_verified", "checked_at")
                                             if field in server})
                    elif server.get("provider") not in ("vidspark", "videoapi") and server.get("embed_url"):
                        episode["servers"].append(server)
                episode["playable_servers"] = list(dict.fromkeys(
                    server["provider"] for server in episode["servers"] if server.get("spanish_audio_verified")))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--limit-shows", type=int, default=0, help="Pilot run; 0 means the full catalog")
    parser.add_argument("--output", type=Path, default=OUTPUT)
    parser.add_argument("--verify-spanish", type=int, default=0,
                        help="Verify VideoAPI HLS audio tracks; -1 means every listed episode, 0 skips")
    parser.add_argument("--refresh-audio", action="store_true")
    args = parser.parse_args()
    if not 1 <= args.workers <= 24 or args.limit_shows < 0 or args.verify_spanish < -1:
        parser.error("workers must be 1–24, limit-shows >= 0, and verify-spanish >= -1")
    key = tmdb_key()
    items, discovered = fetch_discovery(args.workers, args.limit_shows)
    existing = load_existing()
    shows = {}

    def build(item):
        show_id, discovery = item
        metadata = existing.get(show_id)
        if metadata is None:
            metadata = fetch_show(show_id, key)
        return show_id, merge_show(show_id, discovery, metadata)

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        episode_total = 0
        for future in as_completed(pool.submit(build, item) for item in items):
            show_id, show = future.result()
            shows[show_id] = show
            episode_total += show["episode_count"]
            print(f"\rSeries: {len(shows)}/{len(items)} · episodios: {episode_total}",
                  end="", flush=True)
    print()
    ordered = [shows[show_id] for show_id, _ in items]
    preserve_servers(ordered, args.output)
    if args.verify_spanish:
        apply_audio_verification(ordered, args.workers, args.verify_spanish, args.refresh_audio)
    all_episodes = [episode for show in ordered for season in show["seasons"] for episode in season["episodes"]]
    output = {"schema_version": 1, "source": "vidspark+tmdb+videoapi_metadata",
              "generated_at": datetime.now(timezone.utc).isoformat(),
              "discovery_total": discovered, "show_count": len(ordered),
              "episode_count": len(all_episodes),
              "videoapi_listed_episode_count": sum(any(server["provider"] == "videoapi" for server in episode["servers"])
                  for episode in all_episodes),
              "audio_checked_episode_count": sum(any("checked_at" in server for server in episode["servers"])
                  for episode in all_episodes),
              "spanish_audio_verified_episode_count": sum(bool(episode["playable_servers"]) for episode in all_episodes),
              "audio_policy": "Servers are candidates. playable_servers requires confirmed Spanish audio.",
              "shows": ordered}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix(args.output.suffix + ".tmp")
    temporary.write_text(json.dumps(output, ensure_ascii=False, separators=(",", ":")))
    temporary.replace(args.output)
    if args.limit_shows == 0 and args.output == OUTPUT:
        archive_tmp = ARCHIVE.with_suffix(ARCHIVE.suffix + ".tmp")
        with open(archive_tmp, "wb") as raw:
            with gzip.GzipFile(fileobj=raw, mode="wb", compresslevel=6, mtime=0) as compressed:
                compressed.write(args.output.read_bytes())
        archive_tmp.replace(ARCHIVE)
    print(f"Guardado: {args.output} ({len(ordered)} series)")


if __name__ == "__main__":
    main()
