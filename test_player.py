from array import array
import tempfile
from pathlib import Path
import threading
import time
import unittest
from unittest.mock import patch
import player as playback
import wave
from player import StemPlayer, mix_pcm, STEMS


def fixture(folder, frames=22050, rate=44100):
    for index, stem in enumerate(STEMS):
        with wave.open(str(Path(folder) / (stem + '.wav')), 'wb') as stream:
            stream.setnchannels(2); stream.setsampwidth(2); stream.setframerate(rate)
            stream.writeframes(array('h', [100 * (index + 1), -100 * (index + 1)] * frames).tobytes())


class FakeOutput:
    def __init__(self, rate, channels):
        self.rate, self.channels = rate, channels
        self.submitted = 0
        self.done = 0.0
        self.last = time.monotonic()
        self.paused = False
        self.ends = []

    def position(self):
        now = time.monotonic()
        if not self.paused:
            self.done = min(self.submitted, self.done + (now - self.last) * self.rate)
        self.last = now
        return int(self.done)

    def collect(self):
        position = self.position()
        self.ends = [end for end in self.ends if end > position]
        return len(self.ends)

    def submit(self, data):
        self.position()
        self.submitted += len(data) // (self.channels * 2)
        self.ends.append(self.submitted)

    def pause(self):
        self.position(); self.paused = True

    def resume(self):
        self.last = time.monotonic(); self.paused = False

    def reset(self):
        self.submitted = 0; self.done = 0; self.ends = []; self.last = time.monotonic()

    def close(self):
        self.reset()


def wait_until(predicate, timeout=2):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.01)
    raise AssertionError('player condition timed out')


class MixerTests(unittest.TestCase):
    def test_volume_mute_and_single_final_clamp(self):
        chunks = [array('h', [value, -value]).tobytes() for value in [1000, 2000, 3000, 4000]]
        self.assertEqual(list(array('h', mix_pcm(chunks, [1, .5, 0, 0], .5))), [1000, -1000])
        chunks = [array('h', [value]).tobytes() for value in [30000, 30000, -30000, -30000]]
        self.assertEqual(list(array('h', mix_pcm(chunks, [1] * 4))), [0])
        self.assertEqual(list(array('h', mix_pcm(chunks[:2], [1, 1]))), [32767])

    def test_seek_pause_resume_stop_and_eof(self):
        events = []
        player = StemPlayer(events.append, FakeOutput)
        try:
            with tempfile.TemporaryDirectory() as folder:
                fixture(folder, 44100)
                player.load(folder)
                wait_until(lambda: player.snapshot()['loaded'])
                self.assertEqual(player.snapshot()['duration'], 1)
                player.play()
                wait_until(lambda: player.snapshot()['position'] > .08)
                player.pause()
                wait_until(lambda: not player.snapshot()['playing'])
                position = player.snapshot()['position']
                time.sleep(.08)
                self.assertAlmostEqual(player.snapshot()['position'], position, places=2)
                player.seek(.6)
                wait_until(lambda: abs(player.snapshot()['position'] - .6) < .02)
                self.assertFalse(player.snapshot()['playing'])
                player.play()
                wait_until(lambda: player.snapshot()['position'] > .65)
                player.seek(.85)
                wait_until(lambda: player.snapshot()['position'] >= .85)
                wait_until(lambda: not player.snapshot()['playing'])
                self.assertAlmostEqual(player.snapshot()['position'], 1, places=2)
                player.play()
                wait_until(lambda: player.snapshot()['playing'] and player.snapshot()['position'] < .5)
                player.stop()
                wait_until(lambda: not player.snapshot()['playing'] and player.snapshot()['position'] == 0)
                player.unload()
                wait_until(lambda: not player.snapshot()['loaded'])
        finally:
            player.close()
        self.assertFalse(player.thread.is_alive())
        self.assertFalse([e for e in events if e['kind'] == 'playback_error'])

    def test_older_four_stem_results_still_load(self):
        events = []
        player = StemPlayer(events.append, FakeOutput)
        try:
            with tempfile.TemporaryDirectory() as folder:
                fixture(folder)
                (Path(folder) / 'guitar.wav').unlink()
                player.load(folder)
                wait_until(lambda: player.snapshot()['loaded'])
                available = next(event['available'] for event in events if event['kind'] == 'playback_loaded')
                self.assertNotIn('guitar', available)
                self.assertEqual(len(available), 4)
                player.play()
                wait_until(lambda: player.snapshot()['position'] > .05)
                player.unload()
                wait_until(lambda: not player.snapshot()['loaded'])
        finally:
            player.close()

    def test_queue_primed_and_no_starvation_with_slow_mixing(self):
        outputs = []
        class TrackedOutput(FakeOutput):
            def __init__(self, rate, channels):
                super().__init__(rate, channels)
                self.starts = []
                outputs.append(self)
            def resume(self):
                self.starts.append(len(self.ends))
                super().resume()
        original_mix = playback.mix_pcm
        def slow_mix(*args):
            time.sleep(.015)
            return original_mix(*args)
        events = []
        with patch.object(playback, 'mix_pcm', side_effect=slow_mix):
            player = StemPlayer(events.append, TrackedOutput)
            try:
                with tempfile.TemporaryDirectory() as folder:
                    fixture(folder, 44100 * 2)
                    player.load(folder)
                    wait_until(lambda: player.snapshot()['loaded'])
                    player.play()
                    wait_until(lambda: player.snapshot()['position'] > .6)
                    self.assertEqual(outputs[0].starts[0], playback.BUFFER_COUNT)
                    self.assertEqual(player.snapshot()['underruns'], 0)
                    player.seek(1)
                    wait_until(lambda: player.snapshot()['position'] > 1.2)
                    self.assertEqual(outputs[0].starts[-1], playback.BUFFER_COUNT)
                    self.assertEqual(player.snapshot()['underruns'], 0)
                    player.unload()
                    wait_until(lambda: not player.snapshot()['loaded'])
            finally:
                player.close()
        self.assertFalse([event for event in events if event['kind'] == 'playback_error'])

    def test_mismatched_stems_rejected(self):
        events = []
        player = StemPlayer(events.append, FakeOutput)
        try:
            with tempfile.TemporaryDirectory() as folder:
                fixture(folder)
                with wave.open(str(Path(folder) / 'other.wav'), 'wb') as stream:
                    stream.setnchannels(1); stream.setsampwidth(2); stream.setframerate(22050)
                    stream.writeframes(b'\0' * 100)
                player.load(folder)
                wait_until(lambda: any(e['kind'] == 'playback_error' for e in events))
                self.assertFalse(player.snapshot()['loaded'])
        finally:
            player.close()


if __name__ == '__main__':
    unittest.main()
