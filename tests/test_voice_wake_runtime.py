"""Exercise the actual wake loop without opening a microphone or desktop app."""
from __future__ import annotations

import sys
from types import SimpleNamespace

import numpy as np
import pytest

import recordian.voice_wake as wake


@pytest.fixture
def replay(monkeypatch, tmp_path):
    def run(blocks, *, speech=True, pre_vad=True, enter=2, fail_open=False, cooldown=0.0, frame_ms=20, owner=False):
        state = SimpleNamespace(index=0, clock=100.0, opened=False, feeds=[], resets=0, streams=0, decodes=0, owner_samples=[])
        events, hits = [], []

        class Stream:
            pending = False

            def accept_waveform(self, rate, samples):
                state.feeds.append((state.index, samples.copy()))
                self.pending = True

        class Spotter:
            def __init__(self, **kwargs):
                pass

            def create_stream(self):
                state.streams += 1
                return Stream()

            def reset_stream(self, stream):
                state.resets += 1
                stream.pending = False

            def is_ready(self, stream):
                return stream.pending

            def decode_stream(self, stream):
                state.decodes += 1
                stream.pending = False

            def get_result(self, stream):
                return blocks[state.index - 1].get('keyword', '')

        class Mic:
            def __init__(self, **kwargs):
                state.blocksize = kwargs['blocksize']

            def __enter__(self):
                if fail_open:
                    raise RuntimeError('microphone unavailable')
                state.opened = True
                return self

            def __exit__(self, *args):
                state.opened = False

            def read(self, n):
                if state.index == len(blocks):
                    service._stop.set()
                    return np.zeros((n, 1), dtype=np.float32), False
                block = blocks[state.index]
                state.index += 1
                state.clock += n / 16000
                return np.full((n, 1), block.get('value', 0.2), dtype=np.float32), block.get('overflow', False)

        class Vad:
            def __init__(self, mode):
                pass

            def is_speech(self, pcm, rate):
                return blocks[state.index - 1].get('speech', speech)

        monkeypatch.setitem(sys.modules, 'sounddevice', SimpleNamespace(InputStream=Mic))
        monkeypatch.setitem(sys.modules, 'sherpa_onnx', SimpleNamespace(KeywordSpotter=Spotter))
        monkeypatch.setitem(sys.modules, 'webrtcvad', SimpleNamespace(Vad=Vad))
        if owner:
            def extract(samples, **kwargs):
                state.owner_samples.append(len(samples))
                return [1.0]

            monkeypatch.setattr('recordian.speaker_verify.load_speaker_profile', lambda path: SimpleNamespace(
                feature_version=2, embeddings=[[1.0]], embedding=[1.0],
            ))
            monkeypatch.setattr('recordian.speaker_verify.extract_speaker_embedding', extract)
            monkeypatch.setattr('recordian.speaker_verify.cosine_similarity', lambda a, b: 1.0)
        monkeypatch.setattr(wake, 'time', SimpleNamespace(monotonic=lambda: state.clock))
        monkeypatch.setattr(wake.VoiceWakeService, '_check_model_files', lambda self: None)
        monkeypatch.setattr(wake.VoiceWakeService, '_resolve_keywords_file', lambda self: tmp_path / 'keywords.txt')

        def on_event(event):
            if event.get('message') == 'voice_wake_ready':
                assert state.opened, 'ready must mean the microphone is open'
            events.append(event)

        service = wake.VoiceWakeService(
            model=wake.WakeModelConfig('e', 'd', 'j', 't'),
            runtime=wake.WakeRuntimeConfig(
                enabled=True, prefixes=['嗨'], names=['小二'], cooldown_s=cooldown,
                keyword_score=1.5, keyword_threshold=0.12,
                pre_vad_enabled=pre_vad, pre_vad_frame_ms=frame_ms, pre_vad_enter_frames=enter,
                owner_verify_enabled=owner, owner_window_s=1.8,
            ),
            on_wake=hits.append, on_event=on_event, cache_dir=tmp_path,
            can_listen=lambda: blocks[state.index - 1].get('allowed', True),
        )
        service._run()
        return state, events, hits
    return run


def test_two_vad_frames_open_gate_in_40ms_with_preroll(replay):
    state, _, hits = replay([{'value': 0.1}, {'value': 0.2, 'keyword': '嗨小二'}])
    assert state.blocksize == 320
    assert [index for index, _ in state.feeds] == [2, 2]
    assert [round(float(data[0]), 1) for _, data in state.feeds] == [0.1, 0.2]
    assert hits == ['嗨小二']


def test_silence_does_not_decode(replay):
    state, _, hits = replay([{}] * 500, speech=False)
    assert state.feeds == []
    assert state.decodes == 0
    assert hits == []


def test_interrupted_speech_does_not_open_gate_early(replay):
    state, _, _ = replay([{'speech': True}, {'speech': False}, {'speech': True}])
    assert state.feeds == []


def test_busy_audio_cannot_trigger_and_is_not_replayed(replay):
    state, _, hits = replay([
        {'value': 0.1},
        {'allowed': False, 'keyword': '嗨小二', 'value': 0.8},
        {'allowed': False, 'keyword': '嗨小二', 'value': 0.9},
        {'value': 0.3}, {'value': 0.4, 'keyword': '嗨小二'},
    ])
    assert state.streams == 2  # suspension discards the encoder's old audio too
    assert state.resets == 1  # the valid hit
    assert [round(float(data[0]), 1) for _, data in state.feeds] == [0.3, 0.4]
    assert hits == ['嗨小二']


def test_overflow_discards_partial_keyword(replay):
    state, events, _ = replay([{}, {'overflow': True}, {}, {}])
    assert [index for index, _ in state.feeds] == [4, 4]
    assert state.streams == 2
    assert any(e.get('message') == 'voice_wake_audio_overflow_reset' for e in events)


def test_cooldown_drains_audio_without_repeated_callbacks(replay):
    state, _, hits = replay([{'keyword': '嗨小二'}] * 100, pre_vad=False, cooldown=3.0)
    assert hits == ['嗨小二']
    assert state.decodes == 1


def test_failed_microphone_never_reports_ready(replay):
    _, events, hits = replay([], fail_open=True)
    assert not any(e.get('message') == 'voice_wake_ready' for e in events)
    assert any('microphone unavailable' in e.get('message', '') for e in events)
    assert hits == []


@pytest.mark.parametrize('frame_ms', [10, 20, 30])
def test_owner_window_is_complete_and_bounded_for_all_frame_sizes(replay, frame_ms):
    # More than 100 frames must still preserve the configured 1.8 s window,
    # without accumulating the whole recording or silently losing samples.
    state, _, hits = replay([{}] * 499 + [{'keyword': '嗨小二'}],
                            pre_vad=False, frame_ms=frame_ms, owner=True)
    assert state.owner_samples == [28800]
    assert hits == ['嗨小二']
