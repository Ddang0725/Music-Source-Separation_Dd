"""Separation engine and installer. The GUI itself requires only Python's stdlib."""
from __future__ import annotations
import argparse
from array import array
import wave
import datetime as dt
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import venv

ROOT = Path(__file__).resolve().parent
ENV_PYTHON = ROOT / '.venv' / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
STEMS = ('vocals', 'drums', 'bass', 'guitar', 'other')
MODEL = 'htdemucs_6s'
CREATE_FLAGS = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0


def emit(kind, **values):
    print('@@' + json.dumps(dict(kind=kind, **values), ensure_ascii=False), flush=True)


def environment():
    result = os.environ.copy()
    result.update(PYTHONUTF8='1', PYTHONUNBUFFERED='1', TORCH_HOME=str(ROOT / 'models'),
                  PIP_DISABLE_PIP_VERSION_CHECK='1')
    return result


def run(command, *, cwd=ROOT):
    print('실행: ' + subprocess.list2cmdline([str(p) for p in command]), flush=True)
    tail = []
    with subprocess.Popen([str(p) for p in command], cwd=str(cwd), env=environment(),
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          encoding='utf-8', errors='replace', creationflags=CREATE_FLAGS) as process:
        for line in process.stdout:
            line = line.rstrip()
            if line:
                print(line, flush=True)
                tail.append(line)
                tail = tail[-30:]
        code = process.wait()
    if code:
        raise RuntimeError('작업 프로세스가 실패했습니다 (코드 %s).\n%s' % (code, '\n'.join(tail[-10:])))


def has_nvidia():
    try:
        result = subprocess.run(['nvidia-smi', '--query-gpu=name', '--format=csv,noheader'],
                                capture_output=True, text=True, timeout=8, creationflags=CREATE_FLAGS)
        return result.returncode == 0 and bool(result.stdout.strip())
    except (OSError, subprocess.TimeoutExpired):
        return False


def install(accelerator):
    if not (3, 10) <= sys.version_info[:2] <= (3, 11):
        raise RuntimeError('AI 엔진 설치에는 64비트 Python 3.10 또는 3.11이 필요합니다.')
    if sys.maxsize <= 2**32:
        raise RuntimeError('64비트 Python이 필요합니다.')
    emit('stage', text='전용 AI 환경을 만드는 중…')
    if not ENV_PYTHON.exists():
        venv.EnvBuilder(with_pip=True).create(ROOT / '.venv')
    run([ENV_PYTHON, '-m', 'ensurepip', '--upgrade'])
    pip = [ENV_PYTHON, '-m', 'pip', 'install', '--retries', '0', '--timeout', '15']
    emit('stage', text='설치 도구 준비 중…')
    run(pip + ['pip==24.0', 'setuptools==69.5.1', 'wheel==0.43.0'])
    use_cuda = accelerator == 'cuda' or (accelerator == 'auto' and has_nvidia())
    emit('stage', text=('NVIDIA GPU' if use_cuda else 'CPU') + '용 AI 라이브러리 다운로드 중…')
    index = 'https://download.pytorch.org/whl/' + ('cu118' if use_cuda else 'cpu')
    run(pip + ['torch==2.0.1', 'torchaudio==2.0.2', '--index-url', index])
    emit('stage', text='음원 분리 및 오디오 도구 설치 중…')
    run(pip + ['-r', ROOT / 'requirements.txt'])
    emit('stage', text='분리 모델 다운로드 중… (최초 1회)')
    run([ENV_PYTHON, ROOT / 'engine.py', 'model'])
    emit('installed', text='AI 엔진과 모델 설치 완료')


def probe():
    import torch
    import torchaudio
    import soundfile
    import imageio_ffmpeg
    import demucs
    ffmpeg = Path(imageio_ffmpeg.get_ffmpeg_exe())
    if not ffmpeg.is_file():
        raise RuntimeError('오디오 변환 도구를 찾을 수 없습니다.')
    gpu = torch.cuda.is_available()
    emit('ready', cuda=gpu, gpu=torch.cuda.get_device_name(0) if gpu else '',
         torch=torch.__version__)


def load_model():
    from demucs.pretrained import get_model
    get_model(MODEL)
    emit('stage', text='모델 준비 완료')


def result_name(source):
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', Path(source).stem).strip(' .')[:80]
    return (cleaned or 'audio') + '_' + dt.datetime.now().strftime('%Y%m%d_%H%M%S')


def unique_destination(output, source):
    base = result_name(source)
    candidate = output / base
    index = 2
    while candidate.exists():
        candidate = output / (base + '_' + str(index))
        index += 1
    return candidate


def merge_piano_other(folder):
    """Merge aligned PCM stems in chunks without loading the whole song."""
    other, piano = folder / 'other.wav', folder / 'piano.wav'
    merged = folder / 'other-merged.wav'
    with wave.open(str(other), 'rb') as left, wave.open(str(piano), 'rb') as right:
        if left.getparams()[:4] != right.getparams()[:4] or left.getsampwidth() != 2:
            raise RuntimeError('피아노와 반주 WAV 형식이 서로 다릅니다.')
        with wave.open(str(merged), 'wb') as output:
            output.setparams(left.getparams())
            while True:
                a, b = array('h', left.readframes(8192)), array('h', right.readframes(8192))
                if not a:
                    break
                output.writeframes(array('h', (max(-32768, min(32767, x + y)) for x, y in zip(a, b))).tobytes())
    os.replace(merged, other)
    piano.unlink()


def separate(source, output, device):
    source, output = Path(source).resolve(), Path(output).resolve()
    if not source.is_file():
        raise RuntimeError('선택한 음악 파일을 찾을 수 없습니다.')
    output.mkdir(parents=True, exist_ok=True)
    import torch
    import imageio_ffmpeg
    selected = ('cuda' if torch.cuda.is_available() else 'cpu') if device == 'auto' else device
    if selected == 'cuda' and not torch.cuda.is_available():
        raise RuntimeError('GPU를 사용할 수 없습니다. 장치를 CPU로 선택하거나 GPU용 엔진을 설치해주세요.')
    destination = unique_destination(output, source)
    # Temporary files stay on the output drive so moving a completed result is atomic.
    with tempfile.TemporaryDirectory(prefix='.separating-', dir=str(output)) as temporary:
        work = Path(temporary)
        emit('temporary', path=str(work))
        converted = work / 'input.wav'
        emit('stage', text='음악 파일을 읽는 중…')
        run([imageio_ffmpeg.get_ffmpeg_exe(), '-nostdin', '-hide_banner', '-loglevel', 'error',
             '-y', '-i', source, '-vn', '-ar', '44100', '-ac', '2', '-c:a', 'pcm_s16le', converted])
        command = [sys.executable, '-m', 'demucs', '-n', MODEL, '--device', selected,
                   '--segment', '7', '--shifts', '1', '--jobs', '0', '-o', work / 'separated', converted]
        emit('stage', text=('GPU' if selected == 'cuda' else 'CPU') + '로 보컬·드럼·베이스·기타·반주 분리 중…')
        try:
            run(command)
        except RuntimeError as error:
            if device == 'auto' and selected == 'cuda' and 'out of memory' in str(error).lower():
                emit('stage', text='GPU 메모리가 부족하여 CPU로 다시 시도합니다…')
                command[command.index('--device') + 1] = 'cpu'
                run(command)
                selected = 'cpu'
            else:
                raise
        generated = work / 'separated' / MODEL / 'input'
        for stem in (*STEMS, 'piano'):
            file = generated / (stem + '.wav')
            if not file.is_file() or file.stat().st_size <= 44:
                raise RuntimeError(stem + ' 결과 파일이 생성되지 않았습니다.')
        emit('stage', text='피아노를 나머지 반주와 합치는 중…')
        merge_piano_other(generated)
        for stem in STEMS:
            file = generated / (stem + '.wav')
            if not file.is_file() or file.stat().st_size <= 44:
                raise RuntimeError(stem + ' 결과 파일이 생성되지 않았습니다.')
        (generated / '분리정보.json').write_text(json.dumps({
            'source': str(source), 'model': MODEL, 'device': selected,
            'created': dt.datetime.now().isoformat(), 'stems': list(STEMS),
        }, ensure_ascii=False, indent=2), encoding='utf-8')
        generated.rename(destination)
    emit('result', path=str(destination), stems=list(STEMS), device=selected)


def main():
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest='command', required=True)
    setup = commands.add_parser('install')
    setup.add_argument('--accelerator', choices=['auto', 'cpu', 'cuda'], default='auto')
    commands.add_parser('probe')
    commands.add_parser('model')
    split = commands.add_parser('separate')
    split.add_argument('--input', required=True)
    split.add_argument('--output', required=True)
    split.add_argument('--device', choices=['auto', 'cpu', 'cuda'], default='auto')
    args = parser.parse_args()
    if args.command == 'install':
        install(args.accelerator)
    elif args.command == 'probe':
        probe()
    elif args.command == 'model':
        load_model()
    else:
        separate(args.input, args.output, args.device)


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        emit('error', text=str(error))
        print('문제 해결: 인터넷 연결, 저장 공간, 파일 형식과 AI 엔진 설치 상태를 확인해주세요.', flush=True)
        sys.exit(1)
