"""Probe exact tracks and create browser/TV-compatible H.264/AAC MP4."""
from collections import deque
from functools import lru_cache
import json
import os
import shutil
import subprocess
import threading
import tempfile
import time
from pathlib import Path

import certifi

from scrape_webtor_movies import audio_metadata, MAX_MOVIE_BYTES, TARGET_MOVIE_BYTES

PROFILE = 'h264-aac-faststart-v3'
LEGACY_PROFILES = {'h264-aac-es-v1', 'h264-aac-multi-v2'}
_active = set()
_active_lock = threading.Lock()
_stopping = threading.Event()


def stop_all():
    _stopping.set()
    with _active_lock:
        for video in list(_active):
            if video.process.poll() is None:
                try:
                    video.process.kill()
                except OSError:
                    pass


def ffmpeg_binary(override=None):
    if override:
        return override
    if os.environ.get('FFMPEG_BINARY'):
        return os.environ['FFMPEG_BINARY']
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except ImportError:
        return shutil.which('ffmpeg') or 'ffmpeg'


def validate_tools(binary):
    if not shutil.which('ffprobe'):
        raise ValueError('Falta ffprobe en PATH; instala FFmpeg para verificar las pistas')
    result = subprocess.run([binary, '-hide_banner', '-encoders'], capture_output=True, text=True, timeout=15)
    if result.returncode or not all(name in result.stdout for name in ('libx264', ' aac ')):
        raise ValueError('FFmpeg requiere los codificadores libx264 y AAC')


def https_options(url):
    # Some static builds open the CA bundle through FFmpeg's file protocol.
    return ['-protocol_whitelist', 'file,http,https,tls,tcp,crypto,httpproxy',
            '-tls_verify', '1', '-ca_file', certifi.where()] if str(url).lower().startswith('https://') else []


def probe(url):
    result = subprocess.run(['ffprobe', '-v', 'error', '-rw_timeout', '30000000'] + https_options(url) + [
        '-probesize', '5000000', '-analyzeduration', '5000000', '-show_streams',
        '-show_format', '-of', 'json', url], capture_output=True, timeout=60)
    if result.returncode:
        raise ValueError('No se pudieron verificar las pistas del origen')
    return json.loads(result.stdout)


@lru_cache(maxsize=8)
def has_hdr_filters(binary):
    result = subprocess.run([binary, '-hide_banner', '-filters'], capture_output=True, text=True, timeout=15)
    return all(any(f' {name} ' in line for line in result.stdout.splitlines()) for name in ('zscale', 'tonemap'))


def mp4_language(track):
    language = track['language'].replace('_', '-').split('-')[0]
    codes = dict(es='spa', en='eng', fr='fra', it='ita', de='deu', pt='por', ja='jpn',
                 ko='kor', zh='zho', ar='ara', hi='hin', ru='rus', tr='tur', uk='ukr',
                 pl='pol', nl='nld', sv='swe', no='nor', da='dan', fi='fin', he='heb',
                 th='tha', vi='vie', id='ind', ms='msa', ta='tam', te='tel', bn='ben',
                 ur='urd', fa='fas', cs='ces', hu='hun', ro='ron', el='ell')
    if track['spanish']:
        return 'spa'
    if len(language) == 3 and language.isascii() and language.isalpha():
        return language
    return codes.get(language, 'und')


def spanish_rank(track):
    region = track['language'].replace('_', '-').partition('-')[2]
    latin = region in {'419', 'mx', 'ar', 'bo', 'cl', 'co', 'cr', 'cu', 'do', 'ec',
                       'gt', 'hn', 'ni', 'pa', 'pe', 'pr', 'py', 'sv', 'uy', 've', 'us'}
    latin = latin or any(word in (track['title'] or '').lower() for word in ('latin', 'latam'))
    return not latin, not track['default']


def command(url, metadata, binary, threads=2, max_seconds=None, require_spanish=True):
    videos = [s for s in metadata.get('streams', []) if s.get('codec_type') == 'video'
              and not s.get('disposition', {}).get('attached_pic')]
    tracks = audio_metadata(metadata)
    spanish = [track for track in tracks if track['spanish']]
    if not videos or not tracks or (require_spanish and not spanish):
        raise ValueError('Se requiere video y una pista española verificada')
    video = videos[0]
    if not (0 < video.get('width', 0) <= 1920 and 0 < video.get('height', 0) <= 1080):
        raise ValueError('El origen no corresponde a 1080p o inferior')
    spanish.sort(key=spanish_rank)
    audio = spanish[0] if spanish else tracks[0]
    # Put the preferred Spanish track first for players that ignore default flags.
    # Preserve every other track, including alternative mixes of the same language.
    tracks = [audio] + [track for track in tracks if track['index'] != audio['index']]
    hdr = video.get('color_transfer') in ('smpte2084', 'arib-std-b67')
    duration = float(metadata.get('format', {}).get('duration') or 0)
    audio_bitrate = 192000
    video_bitrate = 6000000
    source_size = float(metadata.get('format', {}).get('size') or 0)
    if duration > 0:
        total_bitrate = TARGET_MOVIE_BYTES * 0.97 * 8 / duration
        audio_bitrate = min(192000, int(total_bitrate * 0.25 / len(tracks)))
        if audio_bitrate < 64000:
            raise ValueError('Demasiadas pistas/duracion para mantener todas dentro del limite de tamaño')
        video_bitrate = min(6000000, int(total_bitrate - audio_bitrate * len(tracks)))
    copy = video.get('codec_name') == 'h264' and video.get('pix_fmt') == 'yuv420p' and not hdr and video.get('level', 999) <= 42
    if source_size and duration and source_size + audio_bitrate * len(tracks) * duration / 8 > MAX_MOVIE_BYTES * 0.97:
        copy = False
    args = [binary, '-nostdin', '-hide_banner', '-loglevel', 'error', '-nostats',
            '-progress', 'pipe:2', '-xerror', '-rw_timeout', '30000000'] + https_options(url) + ['-i', url,
            '-map', f'0:{video["index"]}']
    for track in tracks:
        args += ['-map', f'0:{track["index"]}']
    args += ['-sn', '-dn', '-map_metadata', '-1']
    if max_seconds:
        args += ['-t', str(max_seconds)]
    if copy:
        args += ['-c:v', 'copy']
    else:
        filters = 'format=yuv420p'
        if hdr:
            if not has_hdr_filters(binary):
                raise ValueError('HDR requiere FFmpeg con zscale/tonemap; instala imageio-ffmpeg o usa --ffmpeg')
            filters = ('zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,'
                       'tonemap=tonemap=hable:desat=0,zscale=t=bt709:m=bt709:r=tv,format=yuv420p')
        args += ['-vf', filters, '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '21',
                 '-threads', str(threads), '-profile:v', 'high', '-level:v', '4.1',
                 '-maxrate', str(video_bitrate), '-bufsize', str(video_bitrate * 2), '-g', '60', '-keyint_min', '24']
        rate = video.get('avg_frame_rate', '0/1')
        try:
            numerator, denominator = map(float, rate.split('/'))
            if numerator / denominator > 30:
                args += ['-r', '30']
        except (ValueError, ZeroDivisionError):
            pass
        if hdr:
            args += ['-color_primaries', 'bt709', '-color_trc', 'bt709', '-colorspace', 'bt709']
    args += ['-c:a', 'aac', '-profile:a', 'aac_low', '-b:a', str(audio_bitrate), '-ac', '2', '-ar', '48000']
    audio_tracks = []
    for index, track in enumerate(tracks):
        language = mp4_language(track)
        title = track['title'] or track['language']
        args += [f'-metadata:s:a:{index}', f'language={language}', f'-metadata:s:a:{index}',
                 f'title={title}', f'-metadata:s:a:{index}', f'handler_name={title}',
                 f'-disposition:a:{index}', 'default' if index == 0 else '0']
        audio_tracks.append({'input_index': track['index'], 'output_index': index,
            'source_language': track['language'], 'language': language, 'title': title,
            'default': index == 0})
    args += ['-movflags', '+frag_keyframe+empty_moov+default_base_moof',
             '-frag_duration', '2000000', '-f', 'mp4', 'pipe:1']
    return args, {'video_mode': 'copy' if copy else 'transcode', 'audio_input_index': audio['index'],
                  'audio_tracks': audio_tracks, 'audio_track_count': len(audio_tracks),
                  'target_bytes': TARGET_MOVIE_BYTES, 'max_bytes': MAX_MOVIE_BYTES,
                  'hdr_to_sdr': hdr, 'video_codec': 'h264', 'audio_codec': 'aac', 'audio_channels': 2}


class VideoPipe:
    def __init__(self, args, expected_duration):
        if _stopping.is_set():
            raise ValueError('Proceso detenido por el usuario')
        self.duration = expected_duration
        self.seconds = 0.0
        self.ended = False
        self.errors = deque(maxlen=12)
        self.failure_kind = None
        self.process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        with _active_lock:
            _active.add(self)
            if _stopping.is_set():
                self.process.kill()
        self.reader = threading.Thread(target=self._progress, daemon=True)
        self.reader.start()

    def _progress(self):
        for raw in self.process.stderr:
            line = raw.decode('utf-8', errors='replace').strip()
            if line.startswith('out_time_us='):
                try:
                    self.seconds = max(self.seconds, int(line.split('=', 1)[1]) / 1000000)
                except ValueError:
                    pass
            elif line == 'progress=end':
                self.ended = True
            else:
                if 'Non-monotonic DTS' in line:
                    self.failure_kind = 'timestamps no monotonicos'
                elif any(word in line.lower() for word in ('http error', 'connection reset', 'connection timed out', 'error when loading', 'input/output error')):
                    self.failure_kind = 'error de red al recibir el origen'
                self.errors.append(line)

    def read(self, size):
        return self.process.stdout.read(size)

    def verify(self):
        code = self.process.wait(timeout=30)
        self.reader.join(timeout=5)
        if code or not self.ended or self.seconds < max(0.1, self.duration - 5):
            reason = getattr(self, 'failure_kind', None) or 'conversion incompleta'
            raise ValueError(f'FFmpeg: {reason} (codigo {code}; {self.seconds:.1f}/{self.duration:.1f}s procesados)')

    def close(self):
        if self.process.poll() is None:
            try:
                self.process.kill()
            except OSError:
                pass
        self.process.wait()
        self.reader.join(timeout=5)
        self.process.stdout.close()
        self.process.stderr.close()
        with _active_lock:
            _active.discard(self)


class FinalizedVideo(VideoPipe):
    """Create a seekable faststart MP4 before uploading; remove it on every exit."""
    def __init__(self, args, expected_duration, progress=None, validate_tracks=False, expected_video_rate=None):
        self.temporary = tempfile.TemporaryDirectory(prefix='webtor-mp4-')
        self.file = None
        path = Path(self.temporary.name) / 'movie.mp4'
        try:
            # Keep all codec/maps options, replacing only the fragmented pipe muxer.
            output = args.index('-movflags')
            finalized = args[:output] + ['-movflags', '+faststart', '-fs',
                str(MAX_MOVIE_BYTES + 1), '-f', 'mp4', '-y', str(path)]
            super().__init__(finalized, expected_duration)
            while self.process.poll() is None:
                size = path.stat().st_size if path.exists() else 0
                if size > MAX_MOVIE_BYTES:
                    raise ValueError('El MP4 temporal supera el máximo de 5 GB')
                if progress:
                    progress(stage='Preparando MP4', percent=min(99.9, self.seconds / self.duration * 100),
                        sent=0, total=None, estimated=True, speed=0, eta=None)
                time.sleep(0.2)
            super().verify()
            self.size = path.stat().st_size
            if not 0 < self.size <= MAX_MOVIE_BYTES:
                raise ValueError('Tamaño del MP4 final inválido; no se subió')
            metadata = probe(str(path))
            actual = float(metadata.get('format', {}).get('duration') or 0)
            if abs(actual - expected_duration) > 5:
                raise ValueError('Duración del MP4 final incorrecta; no se subió')
            if validate_tracks:
                streams = [s for s in metadata.get('streams', []) if s.get('codec_type') in ('video', 'audio')]
                durations = [float(s.get('duration') or 0) for s in streams]
                ends = [float(s.get('start_time') or 0) + duration for s, duration in zip(streams, durations)]
                if not durations or min(durations) < expected_duration - 1 or max(ends) - min(ends) > 1:
                    raise ValueError('Una pista del MP4 está incompleta; no se subió')
                if expected_video_rate and expected_video_rate > 0:
                    video = next(s for s in streams if s['codec_type'] == 'video')
                    if int(video.get('nb_frames') or 0) / expected_video_rate < expected_duration - 2:
                        raise ValueError('Faltan fotogramas del video; no se subió')
                if progress:
                    progress(stage='Validando MP4')
                # Decode the end of every track strictly, after finalization.
                checked = subprocess.run([args[0], '-nostdin', '-v', 'error', '-xerror',
                    '-ss', str(max(0, actual - 5)), '-i', str(path), '-map', '0:v', '-map', '0:a',
                    '-f', 'null', '-'], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=60)
                if checked.returncode:
                    raise ValueError('El final del MP4 no se decodifica correctamente; no se subió')
            self.file = path.open('rb')
        except BaseException:
            self.close()
            raise

    def read(self, size):
        return self.file.read(size)

    def close(self):
        try:
            if hasattr(self, 'process'):
                super().close()
        finally:
            if self.file is not None:
                self.file.close()
            self.temporary.cleanup()
