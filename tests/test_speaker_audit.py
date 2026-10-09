from __future__ import annotations

import numpy as np
import pytest

from recordian.speaker_verify import add_speaker_sample, enroll_speaker_profile_from_multiple_wavs


@pytest.mark.parametrize('operation', ['enroll', 'add'])
def test_speaker_enrollment_honors_custom_quality_thresholds(tmp_path, monkeypatch, operation):
    from recordian import speaker_verify as speaker
    samples = np.sin(np.arange(32000) * .1).astype(np.float32) * .1
    monkeypatch.setattr(speaker, '_load_wav_any_f32', lambda _: (samples, 16000))
    monkeypatch.setattr(speaker, 'extract_speaker_embedding', lambda *a, **kw: [1.0, 0.0])
    profile = speaker.SpeakerProfile(embedding=[1.0, 0.0], sample_rate=16000, created_at=0, source='synthetic')
    monkeypatch.setattr(speaker, 'load_speaker_profile', lambda _: profile)
    monkeypatch.setattr(speaker, 'save_speaker_profile', lambda *a: None)
    with pytest.raises(ValueError, match='no_valid_samples|sample_quality_too_low'):
        if operation == 'enroll':
            enroll_speaker_profile_from_multiple_wavs(sample_paths=[tmp_path/'sample.wav'],
                profile_path=tmp_path/'profile', min_quality_rms=.9)
        else:
            add_speaker_sample(sample_path=tmp_path/'sample.wav', profile_path=tmp_path/'profile', min_quality_rms=.9)
