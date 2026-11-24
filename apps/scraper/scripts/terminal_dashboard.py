"""Small thread-safe fixed terminal dashboard, with no scrolling progress log."""
import math
import os
import re
import shutil
import sys
import threading
import time
import unicodedata


def clip(value, width):
    text = re.sub(r'[\x00-\x1f\x7f-\x9f]', ' ', str(value))
    result, used = [], 0
    for char in text:
        size = 0 if unicodedata.combining(char) else 2 if unicodedata.east_asian_width(char) in ('W', 'F') else 1
        if used + size > width:
            break
        result.append(char)
        used += size
    return ''.join(result)


def enable_terminal(stream):
    if not getattr(stream, 'isatty', lambda: False)():
        return False
    if os.name != 'nt':
        return os.environ.get('TERM') != 'dumb'
    try:
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.GetStdHandle.argtypes = [wintypes.DWORD]
        kernel.GetStdHandle.restype = wintypes.HANDLE
        kernel.GetConsoleMode.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.SetConsoleMode.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        mode = wintypes.DWORD()
        handle = kernel.GetStdHandle(wintypes.DWORD(-11))
        return bool(kernel.GetConsoleMode(handle, ctypes.byref(mode)) and kernel.SetConsoleMode(handle, mode.value | 4))
    except (OSError, AttributeError):
        return False


def eta_text(seconds):
    if seconds is None or not math.isfinite(seconds):
        return '--'
    seconds = max(0, int(seconds))
    return f'{seconds//3600}:{seconds//60%60:02d}:{seconds%60:02d}' if seconds >= 3600 else f'{seconds//60:02d}:{seconds%60:02d}'


class NullDashboard:
    def phase(self, text): pass
    def start(self, slot, title): pass
    def update(self, slot, **values): pass
    def finish(self, slot, result): pass


class Dashboard:
    def __init__(self, workers, stream=None, live=None, title='GoFile | MP4 H.264 + AAC multiaudio', byte_progress=False):
        self.title = title
        self.byte_progress = byte_progress
        self.stream = stream or sys.stdout
        self.live = enable_terminal(self.stream) if live is None else live
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.rows = [self.empty() for _ in range(workers)]
        self.status = 'Sincronizando inventario...'
        self.counts = {'uploaded': 0, 'exists': 0, 'failed': 0, 'uncertain': 0}
        self.started = time.monotonic()
        self.thread = None

    @staticmethod
    def empty():
        return dict(title='--', stage='En espera', percent=None, sent=0, total=None,
                    estimated=True, speed=0, eta=None, updated=time.monotonic())

    def phase(self, text):
        with self.lock:
            self.status = text

    def start(self, slot, title):
        with self.lock:
            self.rows[slot] = self.empty() | {'title': title, 'stage': 'Webtor'}

    def update(self, slot, **values):
        with self.lock:
            self.rows[slot].update(values, updated=time.monotonic())

    def finish(self, slot, result):
        status = result['status']
        values = dict(stage={'uploaded': 'Completada', 'exists': 'Ya existe', 'failed': 'Fallo',
                             'uncertain': 'Revisar'}.get(status, status), eta=None, speed=0)
        if status == 'uploaded':
            size = result.get('file', {}).get('size', self.rows[slot]['sent'])
            values.update(percent=100, sent=size, total=size, estimated=False)
        with self.lock:
            self.counts[status] = self.counts.get(status, 0) + 1
            self.rows[slot].update(values, updated=time.monotonic())

    def lines(self, width=100, height=24):
        with self.lock:
            rows = [dict(row) for row in self.rows]
            status, counts = self.status, dict(self.counts)
        capacity = max(1, height - 7)
        pages = max(1, math.ceil(len(rows) / capacity))
        page = int((time.monotonic() - self.started) // 5) % pages
        first = page * capacity
        title_width = max(6, min(38, width - 64))
        output = [self.title, status,
                  f'Workers: {len(rows)} | Subidas: {counts["uploaded"]} | Existentes: {counts["exists"]} | Fallos/revision: {counts["failed"]+counts["uncertain"]}',
                  f'W  {"Pelicula":<{title_width}} {"Estado":<12} {"%/falta*":>9} {"MiB enviados/total":>18} {"MiB/s":>8} {"ETA*":>8}']
        for slot, row in enumerate(rows[first:first+capacity], first):
            done = int(row['percent']) if row['percent'] is not None else None
            percent = '--' if done is None else f'{done}%/{100-done}%'
            total = '--' if row['total'] is None else ('~' if row['estimated'] else '') + f'{row["total"]/1048576:.0f}'
            amount = f'{row["sent"]/1048576:.1f}/{total}'
            stale = time.monotonic() - row['updated'] > 3 and row['stage'] in ('MP4/envio', 'Original/envio')
            speed, eta = (0, None) if stale else (row['speed'], row['eta'])
            output.append(f'{slot+1:02d} {clip(row["title"],title_width):<{title_width}} {row["stage"]:<12} {percent:>9} {amount:>18} {speed:>8.2f} {eta_text(eta):>8}')
        output += ['% por bytes enviados; ETA estimada.' if self.byte_progress else '* % por duracion procesada; ~total MiB y ETA estimados hasta finalizar.',
                   f'Ctrl+C para detener. Pagina {page+1}/{pages}.' if pages > 1 else 'Ctrl+C para detener. Los errores detallados quedan en el estado JSON.']
        return [clip(line, max(1, width-1)) for line in output][:max(1,height-1)]

    def render(self):
        size = shutil.get_terminal_size(fallback=(100, 24))
        frame = '\x1b[H' + '\n'.join('\x1b[2K' + line for line in self.lines(size.columns, size.lines)) + '\x1b[J'
        self.stream.write(frame)
        self.stream.flush()

    def _loop(self):
        while not self.stop.wait(0.5):
            try:
                self.render()
            except (OSError, ValueError):
                break

    def __enter__(self):
        if self.live:
            self.stream.write('\x1b[?1049h\x1b[?25l')
            self.render()
            self.thread = threading.Thread(target=self._loop, daemon=True)
            self.thread.start()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=2)
        if self.live:
            self.stream.write('\x1b[?25h\x1b[?1049l')
        phase = 'Error' if exc_type else self.status if self.status.startswith(('Deten', 'Inventario actualizado')) else 'Finalizado'
        counts = self.counts
        self.stream.write(f'GoFile: {phase} | subidas {counts["uploaded"]}, existentes {counts["exists"]}, fallos/revision {counts["failed"]+counts["uncertain"]}.\n')
        self.stream.flush()


class LiveQueueDisplay:
    """Render immediately and keep the last queue view alive during blocking work."""
    def __init__(self, formatter, state, workers, stream=None, interval=1):
        self.formatter, self.state, self.workers = formatter, state, workers
        self.stream = stream or sys.stdout
        self.interactive = self.stream.isatty()
        self.interval = interval
        self.message = 'Starting queue; reading local state'
        self.changed = time.monotonic()
        self.lock = threading.RLock()
        self.stopped = threading.Event()
        self.thread = None
        if self.interactive:
            self.stream.write('\033[?1049h\033[?25l')
        self.render()

    def update(self, message, state=None, workers=None):
        with self.lock:
            if state is not None:
                self.state = state
            if workers is not None:
                self.workers = workers
            if message != self.message:
                self.message, self.changed = message, time.monotonic()
            if self.interactive:
                self.render()

    def render(self):
        import copy
        with self.lock:
            try:
                snapshot = copy.deepcopy(self.state)
            except RuntimeError:
                return  # A queue mutation raced the copy; retry on the next UI frame.
            elapsed = int(time.monotonic() - self.changed)
            screen = self.formatter(snapshot, self.workers, f'{self.message} | {elapsed}s')
            self.stream.write(('\033[H\033[J' if self.interactive else '') + screen)
            self.stream.flush()

    def start_heartbeat(self):
        if not self.interactive or self.thread is not None:
            return
        def run():
            while not self.stopped.wait(self.interval):
                self.render()
        self.thread = threading.Thread(target=run, daemon=True, name='queue-display')
        self.thread.start()

    def close(self):
        self.stopped.set()
        if self.thread is not None:
            self.thread.join(timeout=2)
        if self.interactive:
            self.stream.write('\033[?25h\033[?1049l')
            self.stream.flush()
