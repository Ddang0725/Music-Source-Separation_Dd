"""Bounded-memory tempo and tonal-key estimates. Requires the engine's NumPy only."""
from contextlib import ExitStack
from pathlib import Path
import wave
import numpy as np

MAJOR = np.array([6.35,2.23,3.48,2.33,4.38,4.09,2.52,5.19,2.39,3.66,2.29,2.88])
MINOR = np.array([6.33,2.68,3.52,5.38,2.60,3.53,2.54,4.75,3.98,2.69,3.34,3.17])
NOTES = ('C','C# / Db','D','D# / Eb','E','F','F# / Gb','G','G# / Ab','A','A# / Bb','B')


def key_from_chroma(chroma):
    chroma = np.asarray(chroma, dtype=float)
    if not np.isfinite(chroma).all() or chroma.sum() <= 1e-9:
        return None
    chroma = chroma / chroma.sum()
    if (chroma > chroma.max() * .15).sum() < 3:
        return None
    centered = chroma - chroma.mean()
    norm = np.linalg.norm(centered)
    if norm < 1e-9:
        return None
    ranked = []
    for mode, profile in [('major', MAJOR), ('minor', MINOR)]:
        profile = profile - profile.mean()
        for root in range(12):
            score = float(np.dot(centered, np.roll(profile, root)) / (norm * np.linalg.norm(profile)))
            ranked.append((score, root, mode))
    ranked.sort(reverse=True)
    best, second = ranked[:2]
    if best[0] < .45:
        return None
    margin = best[0] - second[0]
    return dict(label=f'{NOTES[best[1]]} {best[2]}',
                certainty='낮음' if margin < .06 or best[0] < .65 else '보통' if margin < .15 else '높음',
                alternative=f'{NOTES[second[1]]} {second[2]}')


def spectra(samples, size, hop):
    if len(samples) < size:
        samples = np.pad(samples, (0, size-len(samples)))
    frames = np.lib.stride_tricks.sliding_window_view(samples, size)[::hop]
    return np.abs(np.fft.rfft(frames * np.hanning(size), axis=1))


def onset_correlation(samples, rate):
    if len(samples) < rate * 6 or float(np.max(np.abs(samples))) < 1e-5:
        return None
    hop = 128
    magnitude = np.log1p(spectra(samples, 1024, hop) * 20)
    onset = np.maximum(np.diff(magnitude, axis=0), 0).sum(axis=1)
    onset -= onset.mean()
    if np.dot(onset, onset) < 1e-8:
        return None
    size = 1 << (2 * len(onset) - 1).bit_length()
    transformed = np.fft.rfft(onset, size)
    correlation = np.fft.irfft(transformed * transformed.conj(), size)[:len(onset)]
    correlation /= np.arange(len(onset), 0, -1)
    correlation /= max(float(correlation[0]), 1e-12)
    return correlation, rate / hop


def tempo_from_correlations(correlations):
    if not correlations:
        return None
    rate = correlations[0][1]
    length = min(len(values) for values, _ in correlations)
    correlation = np.mean([values[:length] for values, _ in correlations], axis=0)
    low, high = int(rate*60/240), min(length//3-1, int(rate*60/40))
    candidates = []
    for lag in range(max(low, 2), high+1):
        if correlation[lag] < correlation[lag-1] or correlation[lag] < correlation[lag+1]:
            continue
        bpm = 60*rate/lag
        strength = correlation[lag]
        score = strength + .35*correlation[lag*2] + .15*correlation[lag*3]
        score *= np.exp(-.5*(np.log2(bpm/120)/1.2)**2)
        candidates.append((float(score), float(strength), lag))
    if not candidates:
        return None
    score, strength, lag = max(candidates)
    if strength < .08:
        return None
    # Parabolic interpolation reduces FFT-hop quantization of the reported BPM.
    a,b,c = correlation[lag-1:lag+2]
    denominator = a-2*b+c
    delta = float(np.clip(.5*(a-c)/denominator,-.5,.5)) if abs(denominator)>1e-9 else 0
    bpm = round(float(60*rate/(lag+delta)), 1)
    return dict(value=bpm, alternatives=[round(bpm/2,1),round(bpm*2,1)],
                certainty='낮음' if strength < .2 else '보통' if strength < .5 else '높음',
                periodicity=round(float(np.clip(strength,0,1)),3))


def chroma_of(samples, rate, *, details=False):
    empty = (np.zeros(12),0.0) if details else np.zeros(12)
    if np.max(np.abs(samples), initial=0) < 1e-5:
        return empty
    size = 16384
    magnitude = spectra(samples,size,2048)
    frequencies = np.fft.rfftfreq(size,1/rate)
    bins = np.where((frequencies>=55)&(frequencies<=3000))[0]
    center = magnitude[:,bins]
    peaks = (center>magnitude[:,bins-1]) & (center>=magnitude[:,bins+1])
    peaks &= center > magnitude.max(axis=1,keepdims=True)*.025
    rows, columns = np.where(peaks)
    if not len(rows):
        return empty
    indices = bins[columns]
    left = np.log(np.maximum(magnitude[rows,indices-1],1e-12))
    middle = np.log(np.maximum(magnitude[rows,indices],1e-12))
    right = np.log(np.maximum(magnitude[rows,indices+1],1e-12))
    denominator = left-2*middle+right
    delta = np.divide(.5*(left-right),denominator,out=np.zeros_like(left),where=np.abs(denominator)>1e-9)
    refined = (indices+np.clip(delta,-.5,.5))*rate/size
    midi = 69+12*np.log2(refined/440)
    weights = np.sqrt(magnitude[rows,indices])
    # Circular residuals avoid averaging +49 and -49 cents into zero tuning.
    residual = midi-np.rint(midi)
    tuning_vector = np.sum(weights*np.exp(2j*np.pi*residual))
    concentration = abs(tuning_vector)/max(float(weights.sum()),1e-12)
    tuning = float(np.angle(tuning_vector)/(2*np.pi)) if concentration>.25 else 0.0
    corrected = midi-tuning
    notes = np.rint(corrected).astype(int)
    keep = np.abs(corrected-notes)<.32
    frames = np.zeros((len(magnitude),12))
    np.add.at(frames,(rows[keep],notes[keep]%12),weights[keep])
    frames /= np.maximum(frames.sum(axis=1,keepdims=True),1e-9)
    chroma = frames.sum(axis=0)
    return (chroma,round(tuning*100,1)) if details else chroma


def sample_windows(paths, *, with_positions=False):
    with ExitStack() as stack:
        readers = [stack.enter_context(wave.open(str(path),'rb')) for path in paths]
        first = readers[0]
        rate, channels, frames = first.getframerate(), first.getnchannels(), first.getnframes()
        if frames == 0:
            return
        for reader in readers:
            if reader.getsampwidth()!=2 or reader.getcomptype()!='NONE':
                raise ValueError('분석에는 16비트 PCM WAV가 필요합니다.')
            if (reader.getframerate(),reader.getnchannels(),reader.getnframes())!=(rate,channels,frames):
                raise ValueError('반주 파트의 길이와 오디오 형식이 서로 다릅니다.')
        window = min(frames, rate*24)
        # Overlapping windows cover the ENTIRE song; no quick/sampled mode.
        count = max(1,int(np.ceil((frames-window)/max(window//2,1)))+1)
        starts = np.linspace(0,frames-window,count).astype(int)
        stride = max(1, rate//11025)
        if stride>1:
            positions = np.arange(stride*16+1)-stride*8
            cutoff = .45/stride
            kernel = 2*cutoff*np.sinc(2*cutoff*positions)*np.hanning(len(positions))
            kernel /= kernel.sum()
        for start in starts:
            mixed = None
            for reader in readers:
                reader.setpos(int(start))
                data = np.frombuffer(reader.readframes(window),dtype='<i2').reshape(-1,channels)
                mono = data.astype(np.float32).mean(axis=1) / 32768
                if stride>1:
                    mono = np.convolve(mono,kernel,mode='same')[::stride]
                mixed = mono if mixed is None else mixed+mono
            value = (mixed/len(readers),rate/stride)
            yield (*value,float(start/rate),float((start+window)/rate)) if with_positions else value


def tempo_consensus(original_correlations, drum_correlations):
    original = tempo_from_correlations(original_correlations)
    drums = tempo_from_correlations(drum_correlations)
    if not original and not drums:
        return None
    result = dict(original or drums)
    if original and drums:
        a,b = original['value'],drums['value']
        close = abs(a-b)/max(a,b)<.035
        related = min(abs(a-b*2)/max(a,b*2),abs(a*2-b)/max(a*2,b))<.035
        if close:
            result['value'] = round((a+2*b)/3,1)
            result['cross_check'] = '원곡·드럼 일치'
        else:
            if not related and drums['periodicity']>original['periodicity']+.15:
                result = dict(drums)
            result['certainty'] = '낮음'
            result['cross_check'] = '절반·두 배 후보 모호' if related else '원곡·드럼 불일치'
            result['drum_candidate'] = b
    else:
        result['cross_check'] = '원곡만 분석' if original else '드럼만 분석'
    estimates = []
    for correlation in original_correlations+drum_correlations:
        section = tempo_from_correlations([correlation])
        if section:
            value = section['value']
            value = min((value/2,value,value*2),key=lambda x:abs(x-result['value']))
            estimates.append(value)
    result['tempo_varies'] = bool(len(estimates)>2 and np.percentile(estimates,90)-np.percentile(estimates,10)>result['value']*.08)
    if result['tempo_varies']:
        result['certainty'] = '낮음'
    result['alternatives'] = [round(result['value']/2,1),round(result['value']*2,1)]
    return result


def analyze_files(original=None, accompaniment=(), drums=None):
    result = dict(schema=2,mode='precise',estimated=True,bpm=None,key=None,key_sections=[],
                  note='정밀 분석 추정값입니다. 확실도는 정답 확률이 아니며 BPM 절반·두 배 및 전조·관계조에 주의하세요.')
    correlations = []
    if original is not None:
        for samples,rate in sample_windows([original]):
            correlation = onset_correlation(samples,rate)
            if correlation is not None:
                correlations.append(correlation)
    drum_correlations = []
    if drums is not None:
        for samples,rate in sample_windows([drums]):
            correlation = onset_correlation(samples,rate)
            if correlation is not None:
                drum_correlations.append(correlation)
    result['bpm'] = tempo_consensus(correlations,drum_correlations)
    if accompaniment:
        chroma = np.zeros(12)
        tunings = []
        for samples,rate,start,end in sample_windows(accompaniment,with_positions=True):
            values,tuning = chroma_of(samples,rate,details=True)
            chroma += values/max(float(values.sum()),1e-9)
            section = key_from_chroma(values)
            if section:
                tunings.append(tuning)
                result['key_sections'].append(dict(start=round(start,2),end=round(end,2),
                                                    label=section['label'],tuning_cents=tuning))
        result['key'] = key_from_chroma(chroma)
        if result['key']:
            sections = result['key_sections']
            agreements = sum(section['label']==result['key']['label'] for section in sections)
            ratio = agreements/max(len(sections),1)
            result['key']['section_agreement'] = round(ratio,3)
            result['key']['tuning_cents'] = round(float(np.median(tunings)),1) if tunings else 0
            result['key']['possible_modulation'] = bool(len(sections)>=3 and ratio<.75)
            if ratio<.6:
                result['key']['certainty'] = '낮음'
    result['bpm_source'] = '원곡·분리 드럼' if correlations and drum_correlations else '원곡' if correlations else '분리 드럼' if drum_correlations else '박자 정보 부족'
    result['key_source'] = '분리된 반주 (베이스·기타·나머지 악기)' if accompaniment else '분리 후 분석 가능'
    return result
