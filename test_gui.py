"""GUI lifecycle smoke test. No model inference or audio playback is performed."""
import time
import gc
import tempfile
from types import SimpleNamespace
from test_player import fixture, FakeOutput
import unittest
from unittest.mock import patch
from pathlib import Path
from app import App, bootstrap_python


class GuiTests(unittest.TestCase):
    def test_missing_engine_and_cancel(self):
        missing_engine = patch('app.ENV_PYTHON', Path('__missing_engine_for_gui_test__.exe'))
        missing_engine.start()
        app = App()
        app.withdraw()
        def pump(seconds):
            end = time.monotonic() + seconds
            while time.monotonic() < end:
                app.update()
                time.sleep(0.01)
        try:
            pump(2)
            self.assertFalse(app.busy)
            self.assertEqual(str(app.install_button['state']), 'normal')
            if not app.engine_ready:
                self.assertEqual(str(app.start_button['state']), 'disabled')
            app.launch('probe', [bootstrap_python(), '-u', '-c',
                       'import time; print("cancel test",flush=True); time.sleep(30)'])
            for _ in range(30):
                pump(.1)
                if app.process is not None:
                    break
            self.assertTrue(app.busy)
            self.assertTrue(getattr(app.process, 'job_handle', None))
            app.cancel()
            pump(2)
            self.assertFalse(app.busy)
            self.assertIn('취소', app.state_text.get())
        finally:
            if app.process and app.process.poll() is None:
                app.process.kill()
            app.player.close()
            app.destroy()
            missing_engine.stop()
            gc.collect()

    def test_five_stem_mixer_controls_seek_and_layout(self):
        temporary = tempfile.TemporaryDirectory()
        with patch('app.ENV_PYTHON', Path('__missing_engine_for_gui_test__.exe')):
            app = App()
            app.attributes('-alpha', 0)
            app.player.output_factory = FakeOutput
            def pump(seconds):
                end = time.monotonic() + seconds
                while time.monotonic() < end:
                    app.update()
                    time.sleep(.01)
            try:
                pump(.35)
                fixture(temporary.name, 44100)
                app.result_dir = Path(temporary.name)
                app.player.load(temporary.name)
                pump(.25)
                self.assertEqual(len(app.stem_buttons), 5)
                self.assertEqual(str(app.play_button['state']), 'normal')
                app.part_volumes['vocals'].set(25)
                app.update_mix()
                self.assertEqual(app.player.gains[0], .25)
                app.listen('guitar')
                pump(.08)
                self.assertEqual(app.player.gains, [0, 0, 0, 1, 0])
                app.listen_all()
                self.assertEqual(app.player.gains, [.25, 1, 1, 1, 1])
                app.player.pause()
                pump(.08)
                event = SimpleNamespace(x=app.timeline.winfo_width() // 2)
                app.time_press(event)
                app.time_release(event)
                pump(.08)
                self.assertAlmostEqual(app.player.snapshot()['position'], .5, delta=.03)
                app.stop_audio()
                pump(.08)
                for size in ['900x870', '850x840']:
                    app.geometry(size)
                    pump(.08)
                    def check(parent):
                        for child in parent.winfo_children():
                            if child.winfo_manager() and child.winfo_ismapped():
                                self.assertLessEqual(child.winfo_y() + child.winfo_height(), parent.winfo_height() + 2, str(child))
                                self.assertLessEqual(child.winfo_x() + child.winfo_width(), parent.winfo_width() + 2, str(child))
                                check(child)
                    check(app)
            finally:
                app.player.close()
                app.destroy()
                temporary.cleanup()
                gc.collect()


if __name__ == '__main__':
    unittest.main()
