"""음원분리댕 desktop GUI. Only standard-library modules are imported here."""
from __future__ import annotations
import json
import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import queue
import shutil
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from player import StemPlayer

ROOT = Path(__file__).resolve().parent
ENV_PYTHON = ROOT / '.venv' / 'Scripts' / 'python.exe'
FLAGS = subprocess.CREATE_NO_WINDOW
BG, CARD, FIELD = '#191922', '#242430', '#302c40'
TEXT, MUTED, PURPLE = '#f1edf9', '#b4aabe', '#baa3ff'
PARTS = [('vocals', '보컬', '노래하는 목소리'), ('drums', '드럼', '드럼과 타악기'),
         ('bass', '베이스', '낮은 음역의 베이스'), ('guitar', '기타', '기타 소리'),
         ('other', '반주', '피아노·나머지 악기')]


def bootstrap_python():
    current = Path(sys.executable)
    if current.name.lower() == 'pythonw.exe':
        current = current.with_name('python.exe')
    # Installation should create a project env from the original Python, not from itself.
    original = getattr(sys, '_base_executable', None)
    if original:
        candidate = Path(original)
        if candidate.name.lower() == 'pythonw.exe':
            candidate = candidate.with_name('python.exe')
        if candidate.exists():
            return candidate
    return current


class JobLimits(ctypes.Structure):
    _fields_ = [('user_time', ctypes.c_longlong), ('kernel_time', ctypes.c_longlong),
                ('flags', wintypes.DWORD), ('minimum', ctypes.c_size_t), ('maximum', ctypes.c_size_t),
                ('active', wintypes.DWORD), ('affinity', ctypes.c_size_t),
                ('priority', wintypes.DWORD), ('scheduling', wintypes.DWORD)]


class JobInfo(ctypes.Structure):
    _fields_ = [('basic', JobLimits), ('io', ctypes.c_ulonglong * 6),
                ('process_memory', ctypes.c_size_t), ('job_memory', ctypes.c_size_t),
                ('peak_process', ctypes.c_size_t), ('peak_job', ctypes.c_size_t)]


kernel = ctypes.WinDLL('kernel32', use_last_error=True)
kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
kernel.CreateJobObjectW.restype = wintypes.HANDLE
kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
kernel.SetInformationJobObject.restype = wintypes.BOOL
kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
kernel.AssignProcessToJobObject.restype = wintypes.BOOL
kernel.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
kernel.TerminateJobObject.restype = wintypes.BOOL
kernel.CloseHandle.argtypes = [wintypes.HANDLE]
kernel.CloseHandle.restype = wintypes.BOOL


def attach_job(process):
    job = kernel.CreateJobObjectW(None, None)
    if not job:
        return
    info = JobInfo()
    info.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not kernel.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)) or not kernel.AssignProcessToJobObject(job, int(process._handle)):
        kernel.CloseHandle(job)
        return
    process.job_handle = job


def terminate_tree(process):
    if process.poll() is not None:
        return
    job = getattr(process, 'job_handle', None)
    if job and kernel.TerminateJobObject(job, 1):
        return
    try:
        subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'],
                       capture_output=True, creationflags=FLAGS, timeout=10)
    finally:
        if process.poll() is None:
            process.kill()


def cleanup_temporary(path, output):
    if not path or not output:
        return
    target, parent = Path(path).resolve(), Path(output).resolve()
    if target.parent != parent or not target.name.startswith('.separating-'):
        return
    if target.is_dir():
        shutil.rmtree(target)


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title('음원분리댕')
        self.geometry('900x870')
        self.minsize(850, 840)
        self.configure(bg=BG)
        self.option_add('*Font', '{Malgun Gothic} 10')
        self.option_add('*Listbox.background', FIELD)
        self.option_add('*Listbox.foreground', TEXT)
        self.option_add('*Listbox.selectBackground', '#5c477f')
        self.protocol('WM_DELETE_WINDOW', self.close)
        self.events = queue.Queue()
        self.player = StemPlayer(self.events.put)
        self.part_volumes = {key: tk.DoubleVar(value=100) for key, _, _ in PARTS}
        self.part_mutes = {key: tk.BooleanVar(value=False) for key, _, _ in PARTS}
        self.master_volume = tk.DoubleVar(value=80)
        self.time_value = tk.DoubleVar(value=0)
        self.time_text = tk.StringVar(value='00:00 / 00:00')
        self.dragging_time = False
        self.transport_buttons = []
        self.part_controls = []
        self.process = None
        self.busy = False
        self.cancelled = threading.Event()
        self.engine_ready = False
        self.result_dir = None
        self.analysis_result_dir = None
        self.analysis_text = tk.StringVar(value='BPM — · 메인 키 — (정밀 분석)')
        self.source = tk.StringVar()
        self.output = tk.StringVar(value=str(ROOT / '결과'))
        self.device = tk.StringVar(value='자동 (GPU 우선)')
        self.state_text = tk.StringVar(value='AI 엔진 확인 중…')
        self.engine_text = tk.StringVar(value='설치 상태 확인 중')
        self.elapsed_text = tk.StringVar(value='')
        self.started = 0
        self.mode = None
        self.stem_buttons = []
        self.actions = []
        self.setup_style()
        self.build()
        self.set_busy(False)
        self.after(100, self.drain)
        self.after(500, self.tick)
        self.after(200, self.check_engine)

    def setup_style(self):
        style = ttk.Style(self)
        style.theme_use('clam')
        style.configure('Dark.TCombobox', fieldbackground=FIELD, background=FIELD,
                        foreground=TEXT, arrowcolor=PURPLE, bordercolor=FIELD,
                        lightcolor=FIELD, darkcolor=FIELD, padding=6)
        style.map('Dark.TCombobox', fieldbackground=[('readonly', FIELD)],
                  foreground=[('readonly', TEXT), ('disabled', MUTED)])
        style.configure('Mixer.Horizontal.TScale', background=CARD, troughcolor=FIELD, bordercolor=FIELD, lightcolor=PURPLE, darkcolor=PURPLE)
        style.configure('Purple.Horizontal.TProgressbar', troughcolor=FIELD,
                        background=PURPLE, bordercolor=FIELD, lightcolor=PURPLE, darkcolor=PURPLE)

    def label(self, parent, text='', *, color=TEXT, size=10, bold=False, **kwargs):
        return tk.Label(parent, text=text, bg=parent.cget('bg'), fg=color,
                        font=('Malgun Gothic', size, 'bold' if bold else 'normal'), **kwargs)

    def button(self, parent, text, command, *, primary=False):
        return tk.Button(parent, text=text, command=command, relief='flat', bd=0,
                         bg=PURPLE if primary else FIELD, fg='#20182f' if primary else TEXT,
                         activebackground='#9474d2', activeforeground=TEXT,
                         disabledforeground='#746e82', cursor='hand2', padx=15, pady=8)

    def entry_row(self, parent, title, variable, command):
        self.label(parent, title, color=MUTED).pack(anchor='w', pady=(0, 5))
        row = tk.Frame(parent, bg=CARD)
        row.pack(fill='x', pady=(0, 13))
        entry = tk.Entry(row, textvariable=variable, readonlybackground=FIELD, fg=TEXT,
                         relief='flat', state='readonly', insertbackground=TEXT)
        entry.pack(side='left', fill='x', expand=True, ipady=10)
        button = self.button(row, '선택', command)
        button.pack(side='right', padx=(8, 0))
        self.actions.append(button)

    def build(self):
        outer = tk.Frame(self, bg=BG)
        outer.pack(fill='both', expand=True, padx=24, pady=18)
        self.label(outer, '음원분리댕', size=25, bold=True).pack(anchor='w')
        analysis_row = tk.Frame(outer,bg=BG)
        analysis_row.pack(fill='x',pady=(2,10))
        self.label(analysis_row,textvariable=self.analysis_text,color=PURPLE,size=10).pack(side='left')
        self.analysis_button = self.button(analysis_row,'BPM·키 정밀 분석',self.start_analysis)
        self.analysis_button.configure(pady=4)
        self.analysis_button.pack(side='right')
        top = tk.Frame(outer, bg=BG)
        top.pack(fill='x')
        left = tk.Frame(top, bg=CARD, padx=16, pady=13)
        left.pack(side='left', fill='both', expand=True)
        right = tk.Frame(top, bg=CARD, padx=16, pady=13, width=260)
        right.pack(side='right', fill='both', padx=(14, 0))
        self.entry_row(left, '음악 파일 · MP3 / WAV / FLAC / M4A 등', self.source, self.choose_source)
        self.entry_row(left, '결과를 저장할 폴더', self.output, self.choose_output)
        row = tk.Frame(left, bg=CARD)
        row.pack(fill='x')
        self.label(row, '처리 장치', color=MUTED).pack(side='left')
        self.devices = ttk.Combobox(row, textvariable=self.device, state='readonly',
                                   values=['자동 (GPU 우선)', 'GPU (NVIDIA)', 'CPU'],
                                   style='Dark.TCombobox', width=21)
        self.devices.pack(side='right')
        self.label(right, 'AI 엔진', size=12, bold=True).pack(anchor='w')
        self.label(right, color=MUTED, textvariable=self.engine_text, wraplength=220,
                   justify='left').pack(anchor='w', pady=(10, 12))
        self.install_button = self.button(right, 'AI 엔진 설치 / 복구', self.install)
        self.install_button.pack(fill='x')
        self.label(right, '처음 한 번 인터넷 연결이 필요해요.\n설치와 모델 다운로드에는\n수 GB의 공간이 필요합니다.', color=MUTED, size=9,
                   justify='left').pack(anchor='w', pady=(12, 0))
        action = tk.Frame(outer, bg=BG)
        action.pack(fill='x', pady=(14, 9))
        self.start_button = self.button(action, '음원 분리 시작', self.start_separation, primary=True)
        self.start_button.pack(side='left', fill='x', expand=True)
        self.cancel_button = self.button(action, '취소', self.cancel)
        self.cancel_button.pack(side='left', padx=(10, 0))
        self.cancel_button.configure(state='disabled')
        self.progress = ttk.Progressbar(outer, mode='indeterminate', style='Purple.Horizontal.TProgressbar')
        self.progress.pack(fill='x', pady=(0, 5))
        row = tk.Frame(outer, bg=BG)
        row.pack(fill='x')
        self.label(row, textvariable=self.state_text, color=PURPLE, anchor='w').pack(side='left', fill='x', expand=True)
        self.label(row, textvariable=self.elapsed_text, color=MUTED).pack(side='right')
        transport = tk.Frame(outer, bg=CARD, padx=12, pady=8)
        transport.pack(fill='x', pady=(10, 5))
        line = tk.Frame(transport, bg=CARD)
        line.pack(fill='x')
        self.play_button = self.button(line, '▶ 재생', self.toggle_playback, primary=True)
        self.play_button.pack(side='left')
        stop = self.button(line, '■ 정지', self.stop_audio)
        stop.pack(side='left', padx=5)
        all_parts = self.button(line, '전체 듣기', self.listen_all)
        all_parts.pack(side='left')
        self.transport_buttons.extend([self.play_button, stop, all_parts])
        self.label(line, textvariable=self.time_text, color=PURPLE).pack(side='right')
        self.timeline = ttk.Scale(transport, from_=0, to=1, variable=self.time_value,
                                  command=self.time_preview, style='Mixer.Horizontal.TScale')
        self.timeline.pack(fill='x', pady=(9, 0))
        self.timeline.bind('<ButtonPress-1>', self.time_press)
        self.timeline.bind('<ButtonRelease-1>', self.time_release)
        self.timeline.bind('<B1-Motion>', self.time_motion)
        self.timeline.bind('<KeyRelease>', self.time_keyboard)
        self.label(outer, '파트별 볼륨 · 원하는 소리만 줄여보세요', size=11, bold=True).pack(anchor='w', pady=(8, 7))
        stems = tk.Frame(outer, bg=BG)
        stems.pack(fill='x')
        for i, (key, name, detail) in enumerate(PARTS):
            card = tk.Frame(stems, bg=CARD, padx=12, pady=10)
            card.grid(row=0, column=i, sticky='nsew', padx=(0 if i == 0 else 5, 0 if i == len(PARTS) - 1 else 5))
            stems.columnconfigure(i, weight=1, uniform='stem')
            self.label(card, name, size=12, bold=True).pack(anchor='w')
            self.label(card, detail, size=9, color=MUTED).pack(anchor='w', pady=(3, 7))
            button = self.button(card, '단독 듣기', lambda stem=key: self.listen(stem))
            button.pack(fill='x')
            button.configure(state='disabled')
            self.stem_buttons.append(button)
            percent = tk.StringVar(value='100%')
            scale = ttk.Scale(card, from_=0, to=100, variable=self.part_volumes[key],
                              style='Mixer.Horizontal.TScale',
                              command=lambda value, label=percent: (label.set(f'{float(value):.0f}%'), self.update_mix()))
            scale.pack(fill='x', pady=(9, 2))
            line = tk.Frame(card, bg=CARD)
            line.pack(fill='x')
            mute = tk.Checkbutton(line, text='음소거', variable=self.part_mutes[key], command=self.update_mix,
                                  bg=CARD, fg=TEXT, selectcolor=FIELD, activebackground=CARD,
                                  activeforeground=PURPLE, relief='flat', bd=0, highlightthickness=0)
            mute.pack(side='left')
            self.label(line, textvariable=percent, color=MUTED, size=9).pack(side='right')
            self.part_controls.extend([scale, mute])
        row = tk.Frame(outer, bg=BG)
        row.pack(fill='x', pady=(8, 8))
        self.label(row, '전체 볼륨', color=MUTED).pack(side='left')
        self.master_scale = ttk.Scale(row, from_=0, to=100, variable=self.master_volume,
                                      command=lambda value: self.update_mix(), style='Mixer.Horizontal.TScale', length=170)
        self.master_scale.pack(side='left', padx=8)
        self.part_controls.append(self.master_scale)
        existing = self.button(row, '기존 결과 열기', self.choose_results)
        existing.pack(side='right', padx=(8, 0))
        self.actions.append(existing)
        self.open_button = self.button(row, '결과 폴더 열기', self.open_results)
        self.open_button.pack(side='right')
        self.open_button.configure(state='disabled')
        self.log = tk.Text(outer, height=3, bg='#14141b', fg=MUTED, insertbackground=TEXT,
                           relief='flat', padx=10, pady=8, wrap='word', state='disabled', font=('Consolas', 9))
        self.log.pack(fill='both', expand=True)
        self.label(outer, '모든 분리는 이 PC에서 진행됩니다. 원곡은 변경하지 않습니다.', color=MUTED, size=9).pack(anchor='w', pady=(6, 0))
        self.set_playback_enabled(False)
        self.after(100, self.refresh_playback)

    def append_log(self, text):
        self.log.configure(state='normal')
        self.log.insert('end', text + '\n')
        if int(self.log.index('end-1c').split('.')[0]) > 600:
            self.log.delete('1.0', '101.0')
        self.log.see('end')
        self.log.configure(state='disabled')

    def choose_source(self):
        file = filedialog.askopenfilename(title='분리할 음악 선택', filetypes=[
            ('오디오 파일', '*.mp3 *.wav *.flac *.m4a *.ogg *.aac *.wma'), ('모든 파일', '*.*')])
        if file:
            self.source.set(file)
            self.analysis_result_dir = None
            self.analysis_text.set('BPM — · 메인 키 — (정밀 분석)')

    def show_analysis(self, report):
        bpm, key = report.get('bpm'), report.get('key')
        tempo = f"약 {bpm['value']:g} BPM" if bpm else 'BPM 판단 어려움'
        tonal = key['label'] if key else '키 판단 어려움'
        if not key and report.get('key_source')=='분리 후 분석 가능':
            tonal = '키는 분리 후 분석'
        uncertain = any(value and value.get('certainty')=='낮음' for value in (bpm,key))
        kind = '정밀 추정' if report.get('mode')=='precise' else '이전 분석 · 재분석 필요'
        self.analysis_text.set(f'{tempo} · {tonal} ({kind}'+(' · 확실도 낮음' if uncertain else '')+')')
        if key and key.get('possible_modulation'):
            self.append_log('구간별 키 후보가 다릅니다. 전조 또는 조성 모호 가능성은 분석결과.json의 key_sections에서 확인하세요.')
        self.append_log(report.get('note',''))

    def load_saved_analysis(self, folder, restore_source=False):
        self.analysis_result_dir = folder
        self.analysis_text.set('BPM — · 메인 키 — (분석 버튼으로 확인)')
        if restore_source:
            self.source.set('')
        try:
            file = folder / '분석결과.json'
            if file.is_file():
                self.show_analysis(json.loads(file.read_text(encoding='utf-8-sig')))
            if restore_source:
                metadata = json.loads((folder / '분리정보.json').read_text(encoding='utf-8-sig'))
                original = metadata.get('source','')
                self.source.set(original if Path(original).is_file() else '')
        except (OSError,ValueError,TypeError,KeyError) as error:
            self.append_log('기존 분석 정보 읽기: '+str(error))

    def start_analysis(self):
        source = self.source.get()
        folder = self.analysis_result_dir
        if not source and folder is None:
            messagebox.showinfo('분석할 음악','원곡 파일 또는 기존 분리 결과를 먼저 선택해주세요.')
            return
        command = [ENV_PYTHON,ROOT/'engine.py','analyze','--work-dir',self.output.get()]
        if source:
            command += ['--input',source]
        if folder:
            command += ['--result-dir',folder]
        self.stop_audio()
        self.launch('analyze',command)

    def choose_output(self):
        folder = filedialog.askdirectory(title='결과 저장 폴더 선택')
        if folder:
            self.output.set(folder)

    def set_busy(self, value):
        self.busy = value
        for button in self.actions + [self.install_button]:
            button.configure(state='disabled' if value else 'normal')
        self.start_button.configure(state='normal' if not value and self.engine_ready else 'disabled')
        self.analysis_button.configure(state='normal' if not value and ENV_PYTHON.is_file() else 'disabled')
        self.devices.configure(state='disabled' if value else 'readonly')
        self.cancel_button.configure(state='normal' if value else 'disabled')
        if value:
            self.progress.start(12)
            self.started = time.monotonic()
        else:
            self.progress.stop()

    def check_engine(self):
        if ENV_PYTHON.is_file():
            self.launch('probe', [ENV_PYTHON, ROOT / 'engine.py', 'probe'])
        else:
            self.engine_text.set('AI 엔진이 아직 설치되지 않았어요.')
            self.state_text.set('AI 엔진 설치 후 음악 파일을 선택해주세요.')
            self.set_busy(False)

    def install(self):
        if not messagebox.askokcancel('AI 엔진 설치', '전용 환경에 AI 엔진과 모델을 다운로드합니다.\n인터넷 연결과 수 GB의 저장 공간이 필요합니다.\n\n선택한 처리 장치에 맞춰 설치할까요?'):
            return
        accelerator = self.device_code()
        self.engine_ready = False
        self.launch('install', [bootstrap_python(), ROOT / 'engine.py', 'install', '--accelerator', accelerator])

    def device_code(self):
        return {'자동 (GPU 우선)': 'auto', 'GPU (NVIDIA)': 'cuda', 'CPU': 'cpu'}[self.device.get()]

    def start_separation(self):
        if not Path(self.source.get()).is_file():
            messagebox.showinfo('음악 선택', '분리할 음악 파일을 먼저 선택해주세요.')
            return
        if not self.output.get().strip():
            return
        self.stop_audio()
        self.player.unload()
        self.set_playback_enabled(False)
        self.result_dir = None
        self.analysis_result_dir = None
        self.analysis_text.set('BPM — · 메인 키 — (분리 후 정밀 분석)')
        for button in self.stem_buttons:
            button.configure(state='disabled')
        self.open_button.configure(state='disabled')
        self.launch('separate', [ENV_PYTHON, ROOT / 'engine.py', 'separate', '--input', self.source.get(),
                                '--output', self.output.get(), '--device', self.device_code()])

    def launch(self, mode, command):
        if self.busy:
            return
        self.mode = mode
        self.cancelled.clear()
        self.set_busy(True)
        self.state_text.set({'probe': 'AI 엔진 확인 중…', 'install': 'AI 엔진 설치 시작…', 'separate': '음원 분리 준비 중…', 'analyze': 'BPM·키 정밀 분석 준비 중…'}[mode])
        output = self.output.get()
        threading.Thread(target=self.worker, args=(mode, command, output), daemon=True).start()

    def worker(self, mode, command, output):
        process, temporary = None, None
        error_text = None
        code = -1
        try:
            env = os.environ.copy()
            env.update(PYTHONUTF8='1', PYTHONUNBUFFERED='1', TORCH_HOME=str(ROOT / 'models'))
            process = subprocess.Popen([str(x) for x in command], cwd=str(ROOT), env=env,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, encoding='utf-8', errors='replace', creationflags=FLAGS)
            attach_job(process)
            self.process = process
            if self.cancelled.is_set():
                terminate_tree(process)
            for line in process.stdout:
                line = line.rstrip()
                if line.startswith('@@'):
                    try:
                        event = json.loads(line[2:])
                        if event.get('kind') == 'temporary':
                            temporary = event['path']
                        if event.get('kind') == 'error':
                            error_text = event.get('text')
                        self.events.put(event)
                    except (ValueError, KeyError):
                        self.events.put(dict(kind='log', text=line))
                elif line:
                    self.events.put(dict(kind='log', text=line))
            code = process.wait()
        except Exception as error:
            error_text = str(error)
        finally:
            if process is not None and process.poll() is None:
                try:
                    terminate_tree(process)
                except Exception:
                    pass
            if process is not None and process.stdout is not None:
                process.stdout.close()
            job = getattr(process, 'job_handle', None)
            if job:
                kernel.CloseHandle(job)
                process.job_handle = None
            self.process = None
            if self.cancelled.is_set():
                try:
                    cleanup_temporary(temporary, output)
                except OSError as error:
                    self.events.put(dict(kind='log', text='임시 파일 정리: ' + str(error)))
            self.events.put(dict(kind='finished', mode=mode, code=code, cancelled=self.cancelled.is_set(), error=error_text))

    def drain(self):
        try:
            while True:
                event = self.events.get_nowait()
                kind = event.get('kind')
                if kind == 'stage':
                    self.state_text.set(event['text'])
                    self.append_log(event['text'])
                elif kind == 'log':
                    self.append_log(event['text'])
                elif kind == 'error':
                    self.append_log(event['text'])
                elif kind == 'ready':
                    self.engine_ready = True
                    self.engine_text.set('설치 완료 · ' + (event['gpu'] if event['cuda'] else 'CPU 사용 가능'))
                elif kind == 'installed':
                    self.engine_ready = True
                    self.append_log(event['text'])
                elif kind == 'result':
                    self.result_dir = Path(event['path'])
                    self.load_saved_analysis(self.result_dir)
                    self.append_log('저장 완료: ' + event['path'])
                    self.player.load(self.result_dir)
                    self.open_button.configure(state='normal')
                elif kind == 'analysis':
                    self.show_analysis(event['report'])
                elif kind == 'playback_loaded':
                    self.timeline.configure(to=event['duration'])
                    self.time_value.set(0)
                    self.set_playback_enabled(True)
                    self.update_mix()
                    if 'guitar' not in event.get('available', []):
                        self.stem_buttons[3].configure(state='disabled')
                        self.append_log('이전 4파트 결과입니다. 기타는 기존 반주에 포함돼 있어요. 별도 기타 파트는 다시 분리하면 생성됩니다.')
                elif kind == 'playback_error':
                    self.set_playback_enabled(False)
                    self.append_log('재생 오류: ' + event['text'])
                    messagebox.showerror('재생 오류', event['text'])
                elif kind == 'finished':
                    self.set_busy(False)
                    if event['cancelled']:
                        self.state_text.set('작업을 취소했습니다.')
                    elif event['code'] != 0:
                        if event['mode'] in ('probe', 'install'):
                            self.engine_ready = False
                            self.engine_text.set('AI 엔진 설치가 필요해요. 설치 / 복구를 눌러주세요.')
                            self.start_button.configure(state='disabled')
                        self.state_text.set('작업 실패 · 아래 기록을 확인해주세요.')
                        if event['error']:
                            self.append_log(event['error'])
                        if event['mode'] != 'probe':
                            messagebox.showerror('작업을 완료하지 못했어요', (event['error'] or '아래 작업 기록을 확인해주세요.')[-1400:])
                    elif event['mode'] == 'install':
                        self.engine_text.set('설치 완료')
                        self.check_engine()
                    elif event['mode'] == 'probe':
                        self.state_text.set('준비 완료 · 음악 파일을 선택해주세요.')
                    elif event['mode'] == 'analyze':
                        self.state_text.set('BPM·키 정밀 분석 완료 · 추정값을 확인해주세요.')
                    else:
                        self.state_text.set('분리 완료 · 각 파트를 들어보세요.')
        except queue.Empty:
            pass
        self.after(100, self.drain)

    def tick(self):
        if self.busy:
            elapsed = int(time.monotonic() - self.started)
            self.elapsed_text.set('%02d:%02d 경과' % (elapsed // 60, elapsed % 60))
        self.after(500, self.tick)

    def cancel(self):
        self.cancelled.set()
        self.cancel_button.configure(state='disabled')
        self.state_text.set('작업을 취소하는 중…')
        process = self.process
        if process is not None:
            def stop():
                try:
                    terminate_tree(process)
                except Exception as error:
                    self.events.put(dict(kind='log', text='취소: ' + str(error)))
            threading.Thread(target=stop, daemon=True).start()

    def set_playback_enabled(self, enabled):
        for control in self.stem_buttons + self.transport_buttons + self.part_controls + [self.timeline]:
            control.configure(state='normal' if enabled else 'disabled')

    def update_mix(self):
        gains = [0 if self.part_mutes[key].get() else self.part_volumes[key].get() / 100 for key, _, _ in PARTS]
        self.player.volumes(gains, self.master_volume.get() / 100)

    def choose_results(self):
        directory = filedialog.askdirectory(title='다섯 파트의 WAV 파일이 들어 있는 결과 폴더 선택')
        if directory:
            self.result_dir = Path(directory)
            self.load_saved_analysis(self.result_dir, restore_source=True)
            self.stop_audio()
            self.set_playback_enabled(False)
            self.player.load(self.result_dir)
            self.open_button.configure(state='normal')

    def listen(self, stem):
        for key, _, _ in PARTS:
            self.part_mutes[key].set(key != stem)
        self.update_mix()
        self.player.play()

    def listen_all(self):
        for mute in self.part_mutes.values():
            mute.set(False)
        self.update_mix()
        self.player.play()

    def toggle_playback(self):
        if self.player.snapshot()['playing']:
            self.player.pause()
        else:
            self.player.play()

    def stop_audio(self):
        self.player.stop()

    @staticmethod
    def format_time(seconds):
        value = max(0, int(seconds))
        return f'{value // 60:02d}:{value % 60:02d}'

    def pointer_time(self, event):
        duration = self.player.snapshot()['duration']
        fraction = max(0, min(1, (event.x - 8) / max(1, self.timeline.winfo_width() - 16)))
        self.time_value.set(fraction * duration)
        self.time_preview(self.time_value.get())

    def time_press(self, event):
        if self.player.snapshot()['loaded']:
            self.dragging_time = True
            self.pointer_time(event)
        return 'break'

    def time_motion(self, event):
        if self.dragging_time:
            self.pointer_time(event)
        return 'break'

    def time_preview(self, value):
        duration = self.player.snapshot()['duration']
        self.time_text.set(self.format_time(float(value)) + ' / ' + self.format_time(duration))

    def time_release(self, event):
        if self.dragging_time:
            self.pointer_time(event)
            self.player.seek(self.time_value.get())
        self.dragging_time = False
        return 'break'

    def time_keyboard(self, event):
        if event.keysym in ('Left', 'Right', 'Home', 'End', 'Up', 'Down', 'Prior', 'Next'):
            self.player.seek(self.time_value.get())

    def refresh_playback(self):
        state = self.player.snapshot()
        if not self.dragging_time:
            self.time_value.set(state['position'])
            self.time_text.set(self.format_time(state['position']) + ' / ' + self.format_time(state['duration']))
        self.play_button.configure(text='Ⅱ 일시정지' if state['playing'] else '▶ 재생')
        self.after(100, self.refresh_playback)

    def open_results(self):
        if self.result_dir and self.result_dir.is_dir():
            os.startfile(str(self.result_dir))

    def destroy(self):
        for timer in self.tk.call('after', 'info'):
            try:
                self.tk.call('after', 'cancel', timer)
            except tk.TclError:
                pass
        self.player.close()
        super().destroy()

    def close(self):
        if self.busy:
            if not messagebox.askyesno('종료', '진행 중인 작업을 취소하고 종료할까요?'):
                return
            self.cancel()
            self.after(100, self.close_when_idle)
        else:
            self.stop_audio()
            self.player.close()
            self.destroy()

    def close_when_idle(self):
        if self.busy:
            self.after(100, self.close_when_idle)
        else:
            self.stop_audio()
            self.player.close()
            self.destroy()


if __name__ == '__main__':
    App().mainloop()
