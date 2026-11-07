"""Aligned five-stem streaming mixer using Windows PCM output; no AI dependencies."""
from array import array
import ctypes as C
from ctypes import wintypes as W
from pathlib import Path
import queue
import threading
import time
import wave

STEMS = ('vocals', 'drums', 'bass', 'guitar', 'other')
BLOCK_FRAMES = 2048
BUFFER_COUNT = 4  # ~186 ms at 44.1 kHz; tolerate Windows scheduler jitter.


def mix_pcm(chunks, gains, master=1.0):
    """Sum in full precision, then clamp once to avoid intermediate clipping."""
    samples = [array('h', data) for data in chunks]
    length = max((len(values) for values in samples), default=0)
    result = array('h', [0]) * length
    active = [(values, gain * master) for values, gain in zip(samples, gains) if gain and master]
    for i in range(length):
        value = round(sum(values[i] * gain for values, gain in active if i < len(values)))
        result[i] = max(-32768, min(32767, value))
    return result.tobytes()


class Format(C.Structure):
    _pack_ = 2
    _fields_ = [('tag', W.WORD), ('channels', W.WORD), ('rate', W.DWORD),
                ('bytes_per_second', W.DWORD), ('alignment', W.WORD), ('bits', W.WORD), ('extra', W.WORD)]


class Header(C.Structure):
    _fields_ = [('data', C.c_void_p), ('length', W.DWORD), ('recorded', W.DWORD),
                ('user', C.c_size_t), ('flags', W.DWORD), ('loops', W.DWORD),
                ('next', C.c_void_p), ('reserved', C.c_size_t)]


class Position(C.Structure):
    _fields_ = [('type', W.UINT), ('value', W.DWORD), ('padding', W.DWORD)]


mm = C.WinDLL('winmm')
mm.waveOutOpen.argtypes = [C.POINTER(W.HANDLE), W.UINT, C.POINTER(Format), C.c_size_t, C.c_size_t, W.DWORD]
for name in ['waveOutPrepareHeader', 'waveOutUnprepareHeader', 'waveOutWrite']:
    getattr(mm, name).argtypes = [W.HANDLE, C.POINTER(Header), W.UINT]
for name in ['waveOutReset', 'waveOutClose', 'waveOutPause', 'waveOutRestart']:
    getattr(mm, name).argtypes = [W.HANDLE]
mm.waveOutGetPosition.argtypes = [W.HANDLE, C.POINTER(Position), W.UINT]
mm.waveOutGetErrorTextW.argtypes = [W.UINT, W.LPWSTR, W.UINT]


def check(code):
    if code:
        message = C.create_unicode_buffer(256)
        mm.waveOutGetErrorTextW(code, message, 256)
        raise RuntimeError('오디오 장치: ' + message.value)


class WaveOutput:
    def __init__(self, rate, channels):
        self.handle = W.HANDLE()
        self.rate, self.channels = rate, channels
        self.buffers = []
        format = Format(1, channels, rate, rate * channels * 2, channels * 2, 16, 0)
        check(mm.waveOutOpen(C.byref(self.handle), 0xffffffff, C.byref(format), 0, 0, 0))

    def collect(self):
        remaining = []
        for header, data in self.buffers:
            if header.flags & 1:  # WHDR_DONE: the driver no longer uses this buffer.
                check(mm.waveOutUnprepareHeader(self.handle, C.byref(header), C.sizeof(header)))
            else:
                remaining.append((header, data))
        self.buffers = remaining
        return len(remaining)

    def submit(self, pcm):
        data = C.create_string_buffer(pcm)
        header = Header(C.cast(data, C.c_void_p), len(pcm), 0, 0, 0, 0, None, 0)
        check(mm.waveOutPrepareHeader(self.handle, C.byref(header), C.sizeof(header)))
        try:
            check(mm.waveOutWrite(self.handle, C.byref(header), C.sizeof(header)))
        except Exception:
            mm.waveOutUnprepareHeader(self.handle, C.byref(header), C.sizeof(header))
            raise
        self.buffers.append((header, data))

    def position(self):
        position = Position(2, 0, 0)  # TIME_SAMPLES
        check(mm.waveOutGetPosition(self.handle, C.byref(position), C.sizeof(position)))
        if position.type == 2:
            return position.value
        if position.type == 4:  # TIME_BYTES
            return position.value // (self.channels * 2)
        if position.type == 1:  # TIME_MS
            return round(position.value * self.rate / 1000)
        raise RuntimeError('오디오 재생 위치를 읽을 수 없습니다.')

    def pause(self):
        check(mm.waveOutPause(self.handle))

    def resume(self):
        check(mm.waveOutRestart(self.handle))

    def reset(self):
        check(mm.waveOutReset(self.handle))
        self.collect()
        if self.buffers:
            raise RuntimeError('오디오 버퍼를 초기화할 수 없습니다.')

    def close(self):
        if self.handle:
            self.reset()
            check(mm.waveOutClose(self.handle))
            self.handle = W.HANDLE()


class StemPlayer:
    def __init__(self, notify=lambda event: None, output_factory=WaveOutput):
        self.notify = notify
        self.output_factory = output_factory
        self.commands = queue.Queue()
        self.lock = threading.Lock()
        self.state = dict(loaded=False, playing=False, position=0.0, duration=0.0)
        self.gains = [1.0] * len(STEMS)
        self.master = 0.8
        self.files = []
        self.output = None
        self.rate, self.channels, self.total = 44100, 2, 0
        self.base, self.cursor = 0, 0
        self.playing = False
        self.needs_resume = False
        self.underruns = 0
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def load(self, directory):
        self.commands.put(('load', Path(directory)))

    def play(self):
        self.commands.put(('play', None))

    def pause(self):
        self.commands.put(('pause', None))

    def stop(self):
        self.commands.put(('stop', None))

    def unload(self):
        self.commands.put(('unload', None))

    def seek(self, seconds):
        self.commands.put(('seek', max(0, float(seconds))))

    def volumes(self, gains, master):
        with self.lock:
            self.gains = [max(0, min(1, float(value))) for value in gains]
            self.master = max(0, min(1, float(master)))

    def snapshot(self):
        with self.lock:
            return self.state.copy()

    def close(self):
        self.commands.put(('close', None))
        self.thread.join(timeout=3)

    def _dispose(self):
        if self.output is not None:
            self.output.close()
            self.output = None
        for reader in self.files:
            if reader is not None:
                reader.close()
        self.files = []
        self.playing = False
        self.needs_resume = False
        self.underruns = 0
        self.base = self.cursor = self.total = 0

    def _seek(self, frame):
        frame = max(0, min(self.total, int(frame)))
        if self.output is not None:
            self.output.reset()
            # Re-prime the entire queue before restarting after a seek.
            self.output.pause()
        self.needs_resume = self.playing
        self.underruns = 0
        for reader in self.files:
            if reader is not None:
                reader.setpos(frame)
        self.base = self.cursor = frame

    def _load(self, folder):
        self._dispose()
        try:
            for key in STEMS:
                file = folder / (key + '.wav')
                self.files.append(None if key == 'guitar' and not file.exists() else wave.open(str(file), 'rb'))
            first = self.files[0]
            shape = (first.getframerate(), first.getnchannels(), first.getnframes())
            for reader in self.files:
                if reader is None:
                    continue
                if reader.getsampwidth() != 2 or reader.getcomptype() != 'NONE':
                    raise ValueError('16비트 PCM WAV 파일을 사용해주세요.')
                if (reader.getframerate(), reader.getnchannels(), reader.getnframes()) != shape:
                    raise ValueError('다섯 파트의 길이, 샘플 속도, 채널 수가 같아야 합니다.')
            self.rate, self.channels, self.total = shape
            if not self.total:
                raise ValueError('재생할 소리가 없습니다.')
        except Exception:
            self._dispose()
            raise
        self.notify(dict(kind='playback_loaded', duration=self.total / self.rate,
                         available=[key for key, reader in zip(STEMS, self.files) if reader is not None]))

    def _publish(self):
        frame = self.base + self.output.position() if self.output is not None else self.base
        with self.lock:
            self.state = dict(loaded=bool(self.files), playing=self.playing,
                              position=min(self.total, frame) / self.rate, duration=self.total / self.rate,
                              underruns=self.underruns)

    def _run(self):
        closing = False
        while not closing:
            try:
                try:
                    command, value = self.commands.get(timeout=0.005 if self.playing else 0.03)
                except queue.Empty:
                    command, value = None, None
                if command in ('close', 'unload'):
                    self._dispose()
                    closing = command == 'close'
                elif command == 'load':
                    self._load(value)
                elif command == 'seek' and self.files:
                    self._seek(round(value * self.rate))
                elif command == 'stop' and self.files:
                    self.playing = False
                    self._seek(0)
                elif command == 'pause' and self.output is not None:
                    self.output.pause()
                    self.playing = False
                    self.needs_resume = False
                elif command == 'play' and self.files:
                    if self.output is not None and self.base + self.output.position() >= self.total:
                        self._seek(0)
                    if self.base >= self.total:
                        self._seek(0)
                    if self.output is None:
                        self.output = self.output_factory(self.rate, self.channels)
                    if not self.playing:
                        self.output.pause()
                        self.needs_resume = True
                    self.playing = True
                if self.playing and self.output is not None:
                    pending = self.output.collect()
                    if pending == 0 and self.cursor > self.base and self.cursor < self.total and not self.needs_resume:
                        self.underruns += 1
                    # Refill ALL free buffers immediately. A queue timeout between
                    # individual writes can round up to a full Windows timer tick.
                    while pending < BUFFER_COUNT and self.cursor < self.total and self.commands.empty():
                        chunks = [reader.readframes(BLOCK_FRAMES) if reader is not None else b'' for reader in self.files]
                        with self.lock:
                            gains, master = self.gains[:], self.master
                        self.output.submit(mix_pcm(chunks, gains, master))
                        self.cursor += len(chunks[0]) // (self.channels * 2)
                        pending += 1
                    if self.needs_resume and pending:
                        self.output.resume()
                        self.needs_resume = False
                    if self.cursor >= self.total and pending == 0:
                        self.playing = False
                        self.needs_resume = False
                self._publish()
            except Exception as error:
                try:
                    self._dispose()
                except Exception:
                    pass
                with self.lock:
                    self.state = dict(loaded=False, playing=False, position=0.0, duration=0.0)
                self.notify(dict(kind='playback_error', text=str(error)))
