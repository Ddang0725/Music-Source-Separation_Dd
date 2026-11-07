import json
from pathlib import Path
import tempfile
import unittest
import wave
try:
    import numpy as np
except ImportError:
    np = None
if np is not None:
    import music_analysis as analysis


@unittest.skipIf(np is None, 'AI 환경의 NumPy가 필요합니다')
class MusicAnalysisTests(unittest.TestCase):
    def test_known_tempo_click_tracks(self):
        rate = 11025
        pulse = np.random.default_rng(42).normal(size=600)*np.exp(-np.arange(600)/100)
        for bpm in (80,100,120,160):
            with self.subTest(bpm=bpm):
                signal = np.zeros(rate*20)
                for tick in np.arange(.3,19,60/bpm):
                    start = round(tick*rate)
                    signal[start:start+len(pulse)] += pulse
                estimate = analysis.tempo_from_correlations([analysis.onset_correlation(signal,rate)])
                self.assertAlmostEqual(estimate['value'],bpm,delta=1)

    def test_transposed_major_minor_profiles(self):
        for mode, profile in [('major',analysis.MAJOR),('minor',analysis.MINOR)]:
            for root in range(12):
                result = analysis.key_from_chroma(np.roll(profile,root))
                self.assertEqual(result['label'],f'{analysis.NOTES[root]} {mode}')

    def test_silence_and_short_audio_do_not_invent_results(self):
        self.assertIsNone(analysis.onset_correlation(np.zeros(11025*10),11025))
        self.assertIsNone(analysis.onset_correlation(np.ones(11025),11025))
        self.assertIsNone(analysis.key_from_chroma(np.zeros(12)))
        self.assertIsNone(analysis.key_from_chroma(np.ones(12)))
        self.assertIsNone(analysis.key_from_chroma(np.eye(12)[0]))

    def test_detuned_chords_are_corrected(self):
        rate = 11025
        t = np.arange(rate*8)/rate
        for cents in (-32,35):
            signal = sum(.2*np.sin(2*np.pi*440*2**((note-69+cents/100)/12)*t) for note in (60,64,67))
            chroma, tuning = analysis.chroma_of(signal,rate,details=True)
            self.assertEqual(analysis.key_from_chroma(chroma)['label'],'C major')
            self.assertAlmostEqual(tuning,cents,delta=3)

    def test_full_song_window_coverage(self):
        with tempfile.TemporaryDirectory() as folder:
            file = Path(folder)/'long.wav'
            with wave.open(str(file),'wb') as output:
                output.setnchannels(1);output.setsampwidth(2);output.setframerate(11025)
                output.writeframes(b'\0'*(11025*120*2))
            windows = [(start,end) for _,_,start,end in analysis.sample_windows([file],with_positions=True)]
            self.assertGreater(len(windows),6)
            self.assertEqual(windows[0][0],0)
            self.assertEqual(windows[-1][1],120)
            for previous,current in zip(windows,windows[1:]):
                self.assertLessEqual(current[0],previous[1])

    def test_original_and_drums_cross_check(self):
        rate = 11025
        signal = np.zeros(rate*20)
        pulse = np.random.default_rng(9).normal(size=500)*np.exp(-np.arange(500)/90)
        for tick in np.arange(.3,19,.5):
            start=round(tick*rate);signal[start:start+500]+=pulse
        correlation = analysis.onset_correlation(signal,rate)
        result = analysis.tempo_consensus([correlation],[correlation])
        self.assertAlmostEqual(result['value'],120,delta=1)
        self.assertEqual(result['cross_check'],'원곡·드럼 일치')
        self.assertEqual(analysis.tempo_consensus([],[correlation])['cross_check'],'드럼만 분석')

    def test_harmonic_signal_and_persisted_result(self):
        import engine
        rate = 11025
        signal = np.zeros(rate*12)
        time = np.arange(len(signal))/rate
        for start,chord in enumerate([(60,64,67),(60,65,69),(59,62,67),(60,64,67)]*3):
            left,right=start*rate,(start+1)*rate
            for note in chord:
                signal[left:right]+=.2*np.sin(2*np.pi*440*2**((note-69)/12)*time[left:right])
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            for stem in ('bass','guitar','other'):
                with wave.open(str(folder/(stem+'.wav')),'wb') as output:
                    output.setnchannels(1); output.setsampwidth(2); output.setframerate(rate)
                    output.writeframes((signal*10000).astype('<i2').tobytes())
            report = engine.analyze_result(None,folder)
            self.assertEqual(report['key']['label'],'C major')
            self.assertEqual(report['mode'],'precise')
            self.assertTrue(report['key_sections'])
            self.assertIsNone(report['bpm'])
            saved = json.loads((folder/'분석결과.json').read_text(encoding='utf-8'))
            self.assertEqual(saved,report)
            self.assertFalse((folder/'분석결과.json.tmp').exists())


if __name__ == '__main__':
    unittest.main()
