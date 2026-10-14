"""Optional real-tokenizer/upstream contracts; no model weights or GPU loaded.

Run with the ASR Python environment and RECORDIAN_CONFUCIUS_SOURCE plus
RECORDIAN_CONFUCIUS_MODEL_DIR pointing to the installed upstream and tokenizer:
    python -m unittest discover -s tests -p test_confucius_upstream_tail.py
"""
from __future__ import annotations

import importlib.util
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace


class UpstreamTailContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        source = os.environ.get("RECORDIAN_CONFUCIUS_SOURCE")
        model_dir = os.environ.get("RECORDIAN_CONFUCIUS_MODEL_DIR")
        if not source or not model_dir:
            raise unittest.SkipTest("requires local Confucius source and tokenizer")
        sys.path.insert(0, source)
        from r2t2 import R2T2ASRModel
        from transformers import AutoTokenizer

        cls.model_class = R2T2ASRModel
        cls.tokenizer = AutoTokenizer.from_pretrained(model_dir, local_files_only=True, fix_mistral_regex=True)
        spec = importlib.util.spec_from_file_location(
            "upstream_contract_server", Path(__file__).parents[1] / "server/confucius_streaming_server.py",
        )
        cls.srv = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.srv)

    def run_tail(self, raw, language, generated, fixed=""):
        import numpy as np

        model = object.__new__(self.model_class)
        model.backend = "vllm"  # The llama adapter inherits this state machine.
        model.processor = SimpleNamespace(tokenizer=self.tokenizer)
        model._build_text_prompt = lambda **kwargs: "PROMPT:"
        calls = []

        def generate(inputs, sampling_params, **kwargs):
            self.assertLessEqual(len(self.tokenizer.encode(generated)), sampling_params.max_tokens)
            calls.append((inputs[0]["prompt"], sampling_params.max_tokens))
            return [SimpleNamespace(outputs=[SimpleNamespace(text=generated)])]

        model.model = SimpleNamespace(generate=generate)
        session = self.srv.StreamingSession(model, language, "")
        # Valid state reached after ordinary streaming; one final PCM sample
        # forces EOS to handle a tail shorter than the normal 160 ms chunk.
        session.state._raw_decoded = raw
        session.state.text = raw.split("<asr_text>")[-1]
        session.state.chunk_id = 1
        session.state.audio_accum = np.zeros(5120, dtype=np.float32)
        session.last_fixed = fixed
        session.feed(b"\0\0")
        try:
            delta, _ = session.finish()
            self.assertEqual(session.state.audio_accum.size, 5121)
            self.assertEqual(session.state.buffer.size, 0)
            self.assertEqual(len(calls), 1)
            return fixed + delta, calls[0]
        finally:
            session.close()

    def test_multitoken_character_tail_stays_complete(self):
        text, (prompt, budget) = self.run_tail("这里有龘字", "Chinese", "龘字。", "这里有")
        self.assertEqual(prompt, "PROMPT:这里有")
        self.assertEqual(text, "这里有龘字。")
        self.assertNotIn("\ufffd", text)
        self.assertGreater(budget, 4)

    def test_incomplete_auto_language_tag_is_not_final_speech(self):
        with self.assertRaisesRegex(RuntimeError, "incomplete automatic-language"):
            self.run_tail("language Chinese<asr_text>今天", None, "")

    def test_auto_language_tag_can_be_regenerated_at_eos(self):
        text, _ = self.run_tail("language Chinese<asr_text>今天", None, "<asr_text>今天。")
        self.assertEqual(text, "今天。")

    def test_explicit_empty_audio_tag_remains_valid(self):
        text, _ = self.run_tail("language None<asr_text>", None, " None<asr_text>")
        self.assertEqual(text, "")
