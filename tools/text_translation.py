from __future__ import annotations

import os
import platform
import re
import threading
from collections import OrderedDict
from typing import List, Optional


DEFAULT_TRANSLATION_MODEL = "Helsinki-NLP/opus-mt-mul-en"
DEFAULT_TRANSLATION_DEVICE = os.environ.get("VIBEVOICE_TRANSLATION_DEVICE", "cpu")
DEFAULT_TRANSLATION_MAX_INPUT_CHARS = 900
DEFAULT_TRANSLATION_THREADS = max(1, int(os.environ.get("VIBEVOICE_TRANSLATION_THREADS", "1")))
SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+|\n+")


def resolve_translation_device(requested: str) -> str:
    if requested != "auto":
        return requested
    try:
        import torch
    except Exception:
        return "cpu"
    if platform.system() == "Darwin" and torch.backends.mps.is_available():
        return "mps"
    if torch.cuda.is_available():
        return "cuda"
    return "cpu"


def chunk_translation_text(text: str, max_chars: int = DEFAULT_TRANSLATION_MAX_INPUT_CHARS) -> List[str]:
    compact = " ".join(text.split()).strip()
    if not compact:
        return []
    if len(compact) <= max_chars:
        return [compact]

    segments = [segment.strip() for segment in SENTENCE_SPLIT_RE.split(compact) if segment.strip()]
    if not segments:
        return [compact[:max_chars].strip()]

    chunks: List[str] = []
    current = ""
    for segment in segments:
        candidate = f"{current} {segment}".strip() if current else segment
        if len(candidate) <= max_chars:
            current = candidate
            continue
        if current:
            chunks.append(current)
            current = ""
        if len(segment) <= max_chars:
            current = segment
            continue
        for index in range(0, len(segment), max_chars):
            piece = segment[index : index + max_chars].strip()
            if piece:
                chunks.append(piece)
    if current:
        chunks.append(current)
    return chunks


class EnglishTextTranslator:
    def __init__(
        self,
        *,
        model_name: str = DEFAULT_TRANSLATION_MODEL,
        device: str = DEFAULT_TRANSLATION_DEVICE,
        max_input_tokens: int = 512,
        cache_size: int = 128,
    ) -> None:
        self.model_name = model_name
        self.requested_device = device
        self.max_input_tokens = max_input_tokens
        self.cache_size = cache_size
        self._device = "cpu"
        self._load_lock = threading.Lock()
        self._model = None
        self._tokenizer = None
        self._cache: OrderedDict[str, str] = OrderedDict()

    def _ensure_loaded(self) -> None:
        if self._model is not None and self._tokenizer is not None:
            return
        with self._load_lock:
            if self._model is not None and self._tokenizer is not None:
                return
            import torch
            from transformers import AutoModelForSeq2SeqLM, AutoTokenizer

            self._device = resolve_translation_device(self.requested_device)
            torch.set_num_threads(DEFAULT_TRANSLATION_THREADS)
            if hasattr(torch, "set_num_interop_threads"):
                try:
                    torch.set_num_interop_threads(DEFAULT_TRANSLATION_THREADS)
                except RuntimeError:
                    pass
            self._tokenizer = self._load_pretrained(AutoTokenizer)
            self._model = self._load_pretrained(AutoModelForSeq2SeqLM)
            self._model.eval()
            if self._device != "cpu":
                self._model.to(self._device)

    def _load_pretrained(self, factory):
        try:
            return factory.from_pretrained(self.model_name, local_files_only=True)
        except Exception:
            return factory.from_pretrained(self.model_name)

    def _cache_get(self, text: str) -> Optional[str]:
        cached = self._cache.get(text)
        if cached is None:
            return None
        self._cache.move_to_end(text)
        return cached

    def _cache_put(self, text: str, translation: str) -> None:
        self._cache[text] = translation
        self._cache.move_to_end(text)
        while len(self._cache) > self.cache_size:
            self._cache.popitem(last=False)

    def _translate_chunk(self, text: str) -> str:
        self._ensure_loaded()
        assert self._model is not None
        assert self._tokenizer is not None

        import torch

        encoded = self._tokenizer(
            text,
            return_tensors="pt",
            truncation=True,
            max_length=self.max_input_tokens,
        )
        if self._device != "cpu":
            encoded = {key: value.to(self._device) for key, value in encoded.items()}

        input_length = int(encoded["input_ids"].shape[-1])
        max_new_tokens = max(96, min(512, input_length * 3))
        with torch.inference_mode():
            generated = self._model.generate(
                **encoded,
                max_new_tokens=max_new_tokens,
                num_beams=2,
            )
        decoded = self._tokenizer.batch_decode(generated, skip_special_tokens=True)
        return str(decoded[0] if decoded else "").strip()

    def translate(self, text: str) -> str:
        compact = " ".join(text.split()).strip()
        if not compact:
            return ""
        cached = self._cache_get(compact)
        if cached is not None:
            return cached

        translated_chunks = []
        for chunk in chunk_translation_text(compact):
            translated = self._translate_chunk(chunk)
            if translated:
                translated_chunks.append(translated)
        result = " ".join(translated_chunks).strip() or compact
        self._cache_put(compact, result)
        return result
