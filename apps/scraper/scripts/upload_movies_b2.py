#!/usr/bin/env python3
"""Upload verified Spanish 1080p direct movie streams to a private B2 bucket.

Requires: pip install boto3; ffmpeg and ffprobe on PATH. Nothing is uploaded
until this command is run explicitly with B2 credentials in the environment.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "output" / "b2-movies"
SPANISH = {"es", "spa", "esp", "spanish", "espanol", "español", "castellano", "latino"}
PART_SIZE = 32 * 1024 * 1024
UPLOAD_WORKERS = 8
RESOLVER_VERSION = 3
STREAMWISH_UA = "Mozilla/5.0 (Linux; Android 12; Android TV) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"


def load_local_env():
    path = ROOT.parents[1] / ".env.local"
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        key, separator, value = line.partition("=")
        if separator and key.strip().isidentifier():
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                value = value[1:-1]
            os.environ.setdefault(key.strip(), value)


def now():
    return datetime.now(timezone.utc).isoformat()


def save_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as tmp:
        json.dump(value, tmp, ensure_ascii=False, indent=2)
        tmp.write("\n")
        tmp.flush()
        os.fsync(tmp.fileno())
        name = tmp.name
    os.replace(name, path)


def load_sources(paths):
    grouped = {}
    for path in paths:
        data = json.loads(path.read_text())
        for movie in data.get("movies", []):
            if movie.get("tmdb_status") not in ("matched", "found_in_page") or not movie.get("tmdb_id"):
                continue
            grouped.setdefault(int(movie["tmdb_id"]), []).append(movie)
    return grouped


def source_fingerprint(records):
    urls = sorted({source.get("url", "") for record in records
                   for source in (record.get("streams") or []) + (record.get("player_embeds") or [])})
    return hashlib.sha256(json.dumps(urls).encode()).hexdigest()


def probe(url):
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", url],
        capture_output=True, text=True, timeout=30, check=True,
    )
    return json.loads(result.stdout)


def is_spanish(value):
    return str(value or "").strip().lower().replace("á", "a") in SPANISH


def radix_word(number, radix):
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    value = ""
    while number:
        value = digits[number % radix] + value
        number //= radix
    return value or "0"


def unpack_packed_hls(embed_url, html):
    """Port of Android TV's OnDemandHls.fromPackedPlayer, without executing JS."""
    provider = urlparse(embed_url).hostname
    if provider not in {"streamwish.to", "vidhidepro.com"}:
        return None
    match = re.search(r"\}\('([\s\S]*?)',(\d+),(\d+),'([\s\S]*?)'\.split\('\|'\)\)\)", html)
    if not match:
        return None
    radix, count = int(match.group(2)), int(match.group(3))
    if not 2 <= radix <= 36 or not 1 <= count <= 1000:
        return None
    words = match.group(4).split("|")
    unpacked = match.group(1)
    for index in range(count - 1, -1, -1):
        word = words[index] if index < len(words) else ""
        if word:
            unpacked = re.sub(r"\b" + radix_word(index, radix) + r"\b", lambda _: word, unpacked)
    source_key = "hls4" if provider == "vidhidepro.com" else "hls2"
    source = re.search(r'"' + source_key + r'"\s*:\s*"([^"]+)"', unpacked)
    if not source:
        return None
    url = source.group(1).replace("\\/", "/")
    if provider == "vidhidepro.com" and url.startswith("/"):
        url = "https://callistanise.com" + url
    parsed = urlparse(url)
    if provider == "vidhidepro.com":
        expiry = re.fullmatch(r"/stream/[A-Za-z0-9_-]+/[A-Za-z0-9_-]+/(\d{10})/\d+/master\.m3u8", parsed.path)
        if parsed.scheme != "https" or parsed.hostname != "callistanise.com" or not expiry:
            return None
        return url if int(expiry.group(1)) > datetime.now(timezone.utc).timestamp() + 300 else None
    query = parse_qs(parsed.query)
    try:
        start, duration = int(query["s"][0]), int(query["e"][0])
    except (KeyError, IndexError, ValueError):
        return None
    if (parsed.scheme != "https" or not parsed.hostname or not parsed.hostname.endswith(".premilkyway.com")
            or not parsed.path.endswith("/master.m3u8") or not 1 <= duration <= 604800
            or start + duration <= datetime.now(timezone.utc).timestamp() + 300):
        return None
    return url


def resolve_embed_hls(embed_url):
    parsed = urlparse(embed_url)
    valid_path = ((parsed.hostname == "streamwish.to" and re.fullmatch(r"/e/[A-Za-z0-9_-]+/?", parsed.path))
                  or (parsed.hostname == "vidhidepro.com" and re.fullmatch(r"/v/[A-Za-z0-9_-]+/?", parsed.path)))
    if parsed.scheme != "https" or not valid_path:
        return None
    target = "https://medixiru.com" + parsed.path if parsed.hostname == "streamwish.to" else embed_url
    request = Request(target, headers={"User-Agent": STREAMWISH_UA})
    with urlopen(request, timeout=15) as response:
        allowed = {"streamwish.to", "medixiru.com"} if parsed.hostname == "streamwish.to" else {"vidhidepro.com", "callistanise.com"}
        if response.status != 200 or urlparse(response.url).hostname not in allowed:
            return None
        html = response.read(1_000_001)
    if len(html) > 1_000_000:
        return None
    return unpack_packed_hls(embed_url, html.decode("utf-8", "replace"))


def hls_advertises_full_hd(url):
    request = Request(url, headers={"User-Agent": STREAMWISH_UA,
                                    "Accept": "application/vnd.apple.mpegurl,application/x-mpegURL,*/*"})
    with urlopen(request, timeout=8) as response:
        if response.status != 200:
            return False
        manifest = response.read(100_001).decode("utf-8", "replace")
    if len(manifest) > 100_000 or not manifest.startswith("#EXTM3U"):
        return False
    heights = [int(height) for height in re.findall(r"RESOLUTION=\d+x(\d+)", manifest)]
    return not heights or max(heights) >= 1080


def embed_candidate(embed_url):
    if urlparse(embed_url).hostname not in {"streamwish.to", "vidhidepro.com"}:
        return None
    try:
        url = resolve_embed_hls(embed_url)
        if not url or not hls_advertises_full_hd(url):
            return None
        info = probe(url)
        videos = [s for s in info.get("streams", []) if s.get("codec_type") == "video"]
        audios = [s for s in info.get("streams", []) if s.get("codec_type") == "audio"]
        if not any(int(s.get("height") or 0) >= 1080 for s in videos):
            return None
        spanish_audio = [s for s in audios if is_spanish((s.get("tags") or {}).get("language"))]
        if not spanish_audio and len(audios) != 1:
            return None
        return url, info, spanish_audio[0]["index"] if spanish_audio else audios[0]["index"], embed_url, "tagged" if spanish_audio else "single_audio_untagged"
    except (OSError, ValueError, KeyError, subprocess.SubprocessError):
        return None


def candidate(movie, records):
    for record in records:
        for stream in record.get("streams") or []:
            url = stream.get("url")
            if not url or urlparse(url).scheme not in ("http", "https"):
                continue
            try:
                info = probe(url)
                videos = [s for s in info.get("streams", []) if s.get("codec_type") == "video"]
                audios = [s for s in info.get("streams", []) if s.get("codec_type") == "audio"]
                if not any(int(s.get("height") or 0) >= 1080 for s in videos):
                    continue
                spanish_audio = [s for s in audios if is_spanish((s.get("tags") or {}).get("language"))]
                source_spanish = is_spanish(stream.get("language")) or is_spanish(record.get("language"))
                if not spanish_audio and not (source_spanish and len(audios) == 1):
                    continue
                return url, info, spanish_audio[0]["index"] if spanish_audio else audios[0]["index"], url, "tagged" if spanish_audio else "source_label"
            except (OSError, ValueError, KeyError, subprocess.SubprocessError):
                continue
    embeds = list(dict.fromkeys(embed.get("url", "") for record in records
                               for embed in record.get("player_embeds") or []
                               if urlparse(embed.get("url", "")).hostname in {"streamwish.to", "vidhidepro.com"}))
    if embeds:
        with ThreadPoolExecutor(max_workers=min(4, len(embeds))) as pool:
            for selected in pool.map(embed_candidate, embeds):
                if selected:
                    return selected
    return None


def download(url, destination, audio_index):
    # Copy streams; the selected Spanish audio is the only audio in the output.
    subprocess.run([
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-i", url,
        "-map", "0:v:0", "-map", f"0:{audio_index}", "-c", "copy", str(destination),
    ], check=True)


def stream_to_b2(s3, bucket, key, url, audio_index, on_progress=None):
    """Remux into a bounded-memory multipart upload; complete only on ffmpeg success."""
    started = s3.create_multipart_upload(Bucket=bucket, Key=key, ContentType="video/x-matroska")
    upload_id = started["UploadId"]
    process = None
    digest = hashlib.sha256()
    size = 0
    parts = []
    try:
        process = subprocess.Popen([
            "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-i", url,
            "-map", "0:v:0", "-map", f"0:{audio_index}", "-c", "copy",
            "-f", "matroska", "pipe:1",
        ], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        with ThreadPoolExecutor(max_workers=UPLOAD_WORKERS) as pool:
            pending = []
            number = 0
            while True:
                block = process.stdout.read(PART_SIZE)
                if not block:
                    break
                number += 1
                size += len(block)
                digest.update(block)
                future = pool.submit(s3.upload_part, Bucket=bucket, Key=key, UploadId=upload_id,
                                     PartNumber=number, Body=block)
                pending.append((number, future))
                if len(pending) >= UPLOAD_WORKERS:
                    part_number, done = pending.pop(0)
                    parts.append({"PartNumber": part_number, "ETag": done.result()["ETag"]})
                    if on_progress:
                        on_progress(size)
            for part_number, done in pending:
                parts.append({"PartNumber": part_number, "ETag": done.result()["ETag"]})
        if process.wait() != 0 or not parts:
            raise RuntimeError("ffmpeg no pudo completar la transmisión")
        s3.complete_multipart_upload(Bucket=bucket, Key=key, UploadId=upload_id,
                                     MultipartUpload={"Parts": sorted(parts, key=lambda part: part["PartNumber"])})
        return size, digest.hexdigest()
    except BaseException:
        if process and process.poll() is None:
            process.kill()
            process.wait()
        s3.abort_multipart_upload(Bucket=bucket, Key=key, UploadId=upload_id)
        raise
    finally:
        if process and process.stdout:
            process.stdout.close()


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for block in iter(lambda: file.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def main():
    load_local_env()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, default=ROOT / "catalog" / "movies.json")
    parser.add_argument("--sources", type=Path, action="append", help="Catálogo *.tmdb.json del scraper; repetible")
    parser.add_argument("--output", type=Path, default=OUTPUT)
    parser.add_argument("--limit", type=int, default=250)
    parser.add_argument("--max-uploads", type=int, help="Detenerse después de este número de subidas nuevas")
    parser.add_argument("--mode", choices=("stream", "local"), default="stream",
                        help="stream evita el archivo temporal; local conserva una copia para reintentos")
    parser.add_argument("--refresh-sources", action="store_true", help="Volver a comprobar fuentes descartadas recientemente")
    args = parser.parse_args()
    if not 1 <= args.limit <= 250:
        parser.error("--limit debe estar entre 1 y 250")
    if args.max_uploads is not None and args.max_uploads < 1:
        parser.error("--max-uploads debe ser positivo")
    source_paths = args.sources or [ROOT / "output" / "cineari-movies.tmdb.json", ROOT / "output" / "cubana-movies.tmdb.json"]
    for path in [args.catalog, *source_paths]:
        if not path.is_file():
            parser.error(f"Falta {path}; genera primero los catálogos TMDB del scraper")
    required = ["B2_KEY_ID", "B2_APPLICATION_KEY", "B2_BUCKET", "B2_ENDPOINT"]
    missing = [name for name in required if not os.environ.get(name)]
    if missing:
        parser.error("Faltan variables: " + ", ".join(missing))
    try:
        import boto3
        from boto3.s3.transfer import TransferConfig
    except ImportError:
        parser.error("Instala la dependencia: python3 -m pip install boto3")
    catalog = json.loads(args.catalog.read_text())
    movies = [item for category in catalog["categories"] for item in category["items"]][:args.limit]
    selected_ids = {str(movie["tmdb_id"]) for movie in movies}
    if len(movies) != args.limit or len({m["tmdb_id"] for m in movies}) != len(movies):
        parser.error("El catálogo no contiene la selección única solicitada")
    sources = load_sources(source_paths)
    endpoint = os.environ["B2_ENDPOINT"].rstrip("/")
    if "://" not in endpoint:
        endpoint = "https://" + endpoint
    parsed_endpoint = urlparse(endpoint)
    if parsed_endpoint.scheme != "https" or not parsed_endpoint.hostname or not parsed_endpoint.hostname.startswith("s3.") or not parsed_endpoint.hostname.endswith(".backblazeb2.com"):
        parser.error("B2_ENDPOINT debe ser una URL HTTPS del endpoint S3 de Backblaze")
    region = parsed_endpoint.hostname.removeprefix("s3.").removesuffix(".backblazeb2.com")
    s3 = boto3.client("s3", endpoint_url=endpoint, region_name=region,
                      aws_access_key_id=os.environ["B2_KEY_ID"],
                      aws_secret_access_key=os.environ["B2_APPLICATION_KEY"])
    bucket = os.environ["B2_BUCKET"]
    args.output.mkdir(parents=True, exist_ok=True)
    manifest = args.output / "movies.json"
    state = json.loads(manifest.read_text()) if manifest.exists() else {"type": "movie", "bucket": bucket, "movies": {}}
    if state["bucket"] != bucket:
        parser.error("El historial existente corresponde a otro bucket")
    # Check the exact upload operation before downloading multi-gigabyte files.
    check_key = "movies/.upload-check"
    try:
        check = s3.create_multipart_upload(Bucket=bucket, Key=check_key)
        s3.abort_multipart_upload(Bucket=bucket, Key=check_key, UploadId=check["UploadId"])
    except Exception as error:
        code = getattr(error, "response", {}).get("Error", {}).get("Code", type(error).__name__)
        parser.error(f"B2 rechazó la comprobación de escritura ({code}); revisa ID, clave, bucket y endpoint")
    try:
        uploaded_this_run = 0
        uploaded_total = sum(state["movies"].get(mid, {}).get("status") == "uploaded" for mid in selected_ids)
        for position, movie in enumerate(movies, 1):
            mid = str(movie["tmdb_id"])
            previous = state["movies"].get(mid, {})
            if previous.get("status") == "uploaded":
                continue
            records = sources.get(int(mid), [])
            fingerprint = source_fingerprint(records)
            try:
                recent_check = datetime.fromisoformat(previous.get("updated_at", "")) > datetime.now(timezone.utc) - timedelta(days=1)
            except ValueError:
                recent_check = False
            if (not args.refresh_sources and previous.get("status") == "no_eligible_source"
                    and previous.get("resolver_version") == RESOLVER_VERSION
                    and previous.get("source_fingerprint") == fingerprint and recent_check):
                continue
            selected = candidate(movie, records)
            if not selected:
                state["movies"][mid] = {"type": "movie", "title": movie["title"], "year": movie["year"],
                                        "status": "no_eligible_source", "resolver_version": RESOLVER_VERSION,
                                        "source_fingerprint": fingerprint, "updated_at": now()}
                save_json(manifest, state)
                continue
            url, _info, audio_index, origin_url, audio_evidence = selected
            print(f"Subiendo {uploaded_total + 1}/{len(movies)}: {movie['title']}", flush=True)
            key = f"movies/{mid}/{mid}.mkv"
            row = {"type": "movie", "tmdb_id": int(mid), "title": movie["title"], "year": movie["year"],
                   "source_url": url, "source_page_url": origin_url, "audio_evidence": audio_evidence,
                   "bucket": bucket, "object_key": key,
                   "b2_url": f"{endpoint}/{bucket}/{key}", "status": "preparing", "updated_at": now()}
            state["movies"][mid] = row
            save_json(manifest, state)
            temp = args.output / f"{mid}.partial.mkv"
            try:
                reusable = (temp.exists() and previous.get("bytes") == temp.stat().st_size
                            and previous.get("sha256") == sha256(temp))
                if args.mode == "stream" and not reusable:
                    if temp.exists():
                        temp.unlink()  # An interrupted local download is not reusable.
                    row.update(status="streaming", updated_at=now())
                    save_json(manifest, state)
                    size, checksum = stream_to_b2(s3, bucket, key, url, audio_index)
                    row.update(bytes=size, sha256=checksum)
                else:
                    if not reusable:
                        row.update(status="downloading", updated_at=now())
                        save_json(manifest, state)
                        download(url, temp, audio_index)
                    # Verify the complete local file before starting or resuming upload.
                    local = probe(str(temp))
                    if not any(s.get("codec_type") == "video" and int(s.get("height") or 0) >= 1080 for s in local["streams"]):
                        raise ValueError("El archivo descargado no es 1080p")
                    if not any(s.get("codec_type") == "audio" for s in local["streams"]):
                        raise ValueError("El archivo descargado no tiene audio")
                    row.update(status="uploading", bytes=temp.stat().st_size, sha256=sha256(temp), updated_at=now())
                    save_json(manifest, state)
                    s3.upload_file(str(temp), bucket, key, ExtraArgs={"ContentType": "video/x-matroska"},
                                   Config=TransferConfig(multipart_threshold=64*1024*1024, multipart_chunksize=64*1024*1024,
                                                         max_concurrency=UPLOAD_WORKERS))
                remote = s3.head_object(Bucket=bucket, Key=key)
                if remote["ContentLength"] != row["bytes"]:
                    raise ValueError("El tamaño remoto no coincide")
                row.update(status="uploaded", updated_at=now())
                save_json(manifest, state)
                if temp.exists():
                    temp.unlink()
                uploaded_this_run += 1
                uploaded_total += 1
                print(f"Subida {uploaded_total}/{len(movies)}: {movie['title']}", flush=True)
                if args.max_uploads is not None and uploaded_this_run >= args.max_uploads:
                    break
            except Exception as error:
                row.update(status="failed", error=str(error), updated_at=now())
                save_json(manifest, state)
                print(f"  Error: {error}", file=sys.stderr)
    except KeyboardInterrupt:
        print("\nInterrumpido; vuelve a ejecutar el mismo comando para reanudar.", file=sys.stderr)
        return 130
    print(f"Resumen: {uploaded_total}/{len(movies)} subidas; "
          f"{sum(state['movies'].get(mid, {}).get('status') == 'no_eligible_source' for mid in selected_ids)} sin fuente elegible.")
    if args.max_uploads is not None and uploaded_this_run < args.max_uploads:
        print(f"No se alcanzaron {args.max_uploads} subidas nuevas; revisa el historial local.", file=sys.stderr)
        return 3
    if args.max_uploads is None and uploaded_total < len(movies):
        return 4
    return 0


if __name__ == "__main__":
    sys.exit(main())
