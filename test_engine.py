"""Fast orchestration tests; these do not claim to test model quality/inference."""
import contextlib
from array import array
import io
import json
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch
import wave
import engine
from app import cleanup_temporary


def write_wav(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), 'wb') as file:
        file.setnchannels(2)
        file.setsampwidth(2)
        file.setframerate(44100)
        file.writeframes(b'\0' * 400)


class SeparationTests(unittest.TestCase):
    def run_separation(self, base, *, oom=False, missing=False, gpu=True, device='auto'):
        source = base / '한글 곡 원본.wav'
        if not source.exists():
            write_wav(source)
        output = base / '결과 폴더'
        calls = []
        state = {'oom': oom}
        def fake_run(command, **kwargs):
            command = [str(p) for p in command]
            calls.append(command)
            if command[0] == 'fake-ffmpeg':
                write_wav(command[-1])
                return
            chosen = command[command.index('--device') + 1]
            if state['oom'] and chosen == 'cuda':
                state['oom'] = False
                raise RuntimeError('CUDA out of memory')
            root = Path(command[command.index('-o') + 1]) / engine.MODEL / 'input'
            for stem in (*engine.STEMS, 'piano'):
                if missing and stem == 'other':
                    continue
                write_wav(root / (stem + '.wav'))
        fake_torch = types.SimpleNamespace(cuda=types.SimpleNamespace(is_available=lambda: gpu))
        fake_ffmpeg = types.SimpleNamespace(get_ffmpeg_exe=lambda: 'fake-ffmpeg')
        events = []
        with patch.dict('sys.modules', {'torch': fake_torch, 'imageio_ffmpeg': fake_ffmpeg}), \
             patch.object(engine, 'run', side_effect=fake_run), \
             patch.object(engine, 'emit', side_effect=lambda kind, **kw: events.append(dict(kind=kind, **kw))):
            engine.separate(source, output, device)
        result = Path(next(event['path'] for event in events if event['kind'] == 'result'))
        return source, result, calls

    def test_five_stems_original_unchanged_and_unique_results(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            source = base / '한글 곡 원본.wav'
            write_wav(source)
            original = source.read_bytes()
            _, first, _ = self.run_separation(base)
            _, second, _ = self.run_separation(base)
            self.assertNotEqual(first, second)
            self.assertEqual(source.read_bytes(), original)
            self.assertEqual({p.stem for p in first.glob('*.wav')}, set(engine.STEMS))
            self.assertEqual(json.loads((first / '분리정보.json').read_text(encoding='utf-8'))['source'], str(source.resolve()))
            self.assertFalse(list((base / '결과 폴더').glob('.separating-*')))

    def test_oom_auto_retries_cpu(self):
        with tempfile.TemporaryDirectory() as folder:
            _, result, calls = self.run_separation(Path(folder), oom=True)
            devices = [c[c.index('--device') + 1] for c in calls if '--device' in c]
            self.assertEqual(devices, ['cuda', 'cpu'])
            self.assertEqual(json.loads((result / '분리정보.json').read_text(encoding='utf-8'))['device'], 'cpu')

    def test_missing_stem_never_publishes_success(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            with self.assertRaisesRegex(RuntimeError, 'other'):
                self.run_separation(base, missing=True)
            self.assertEqual(list((base / '결과 폴더').iterdir()), [])

    def test_forced_gpu_unavailable(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaisesRegex(RuntimeError, 'GPU'):
                self.run_separation(Path(folder), device='cuda', gpu=False)

    def test_missing_input(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaisesRegex(RuntimeError, '찾을 수 없습니다'):
                engine.separate(Path(folder) / 'missing.mp3', Path(folder) / 'out', 'auto')

    def test_cancel_cleanup_rejects_unrelated_directory(self):
        with tempfile.TemporaryDirectory() as folder:
            parent = Path(folder)
            unrelated = parent / 'keep'
            unrelated.mkdir()
            temporary = parent / '.separating-owned'
            temporary.mkdir()
            cleanup_temporary(unrelated, parent)
            self.assertTrue(unrelated.exists())
            cleanup_temporary(temporary, parent)
            self.assertFalse(temporary.exists())

    def test_piano_merged_into_other_and_removed(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for name, values in [('other', [1000, 32760, -32760]), ('piano', [2000, 100, -100])]:
                with wave.open(str(root / (name + '.wav')), 'wb') as file:
                    file.setnchannels(1); file.setsampwidth(2); file.setframerate(44100)
                    file.writeframes(array('h', values).tobytes())
            engine.merge_piano_other(root)
            with wave.open(str(root / 'other.wav'), 'rb') as file:
                self.assertEqual(list(array('h', file.readframes(3))), [3000, 32767, -32768])
            self.assertFalse((root / 'piano.wav').exists())
            self.assertFalse((root / 'other-merged.wav').exists())

    def test_unicode_protocol(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            engine.emit('stage', text='보컬 분리 중')
        self.assertEqual(json.loads(output.getvalue()[2:])['text'], '보컬 분리 중')


if __name__ == '__main__':
    unittest.main()
