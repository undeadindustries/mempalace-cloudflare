"""Local @cf/baai/bge-small-en-v1.5 embeddings via ONNX Runtime.

Workers AI on the free plan caps neurons per day, so a palace import embeds
passages on the machine that already holds them. Vectors match the Worker's
384-dim cosine index: CLS pooling, then L2 normalization, no query prefix
(passages are encoded as-is; the Worker embeds queries the same way).
"""

from __future__ import annotations

import os
import urllib.request
from pathlib import Path

import numpy as np
import onnxruntime as ort
from tokenizers import Tokenizer

MODEL_REPO = "Xenova/bge-small-en-v1.5"
MODEL_FILE = "onnx/model_quantized.onnx"
TOKENIZER_FILE = "tokenizer.json"
MAX_TOKENS = 512
EMBEDDING_DIMENSION = 384
_HF_BASE = "https://huggingface.co"

_MODEL_URL = f"{_HF_BASE}/{MODEL_REPO}/resolve/main/{MODEL_FILE}"
_TOKENIZER_URL = f"{_HF_BASE}/{MODEL_REPO}/resolve/main/{TOKENIZER_FILE}"


def default_cache_dir() -> Path:
    """Directory for the downloaded ONNX weights and tokenizer."""
    return Path(os.path.expanduser("~/.cache/mempalace-cloudflare/bge-small-en-v1.5"))


def _download(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.is_file() and dest.stat().st_size > 0:
        return
    tmp = dest.with_suffix(dest.suffix + ".part")
    request = urllib.request.Request(url, headers={"User-Agent": "mempalace-cloudflare-import"})
    with urllib.request.urlopen(request, timeout=120) as response, tmp.open("wb") as handle:
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            handle.write(chunk)
    tmp.replace(dest)


class BgeSmallOnnx:
    """Encode passages with the quantized bge-small ONNX graph."""

    def __init__(self, cache_dir: Path | None = None):
        folder = cache_dir or default_cache_dir()
        model_path = folder / "model_quantized.onnx"
        tokenizer_path = folder / "tokenizer.json"
        _download(_MODEL_URL, model_path)
        _download(_TOKENIZER_URL, tokenizer_path)
        self._tokenizer = Tokenizer.from_file(str(tokenizer_path))
        self._tokenizer.enable_truncation(max_length=MAX_TOKENS)
        self._tokenizer.enable_padding(pad_id=0, pad_token="[PAD]")
        self._session = ort.InferenceSession(str(model_path), providers=["CPUExecutionProvider"])

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Return one L2-normalized 384-float vector per text."""
        if not texts:
            return []
        encoded = self._tokenizer.encode_batch(texts)
        feeds = {
            "input_ids": np.asarray([item.ids for item in encoded], dtype=np.int64),
            "attention_mask": np.asarray([item.attention_mask for item in encoded], dtype=np.int64),
            "token_type_ids": np.asarray([item.type_ids for item in encoded], dtype=np.int64),
        }
        hidden = self._session.run(None, feeds)[0]
        cls = hidden[:, 0, :]
        norms = np.linalg.norm(cls, axis=1, keepdims=True)
        unit = cls / np.clip(norms, 1e-12, None)
        if unit.shape[1] != EMBEDDING_DIMENSION:
            raise RuntimeError(f"expected {EMBEDDING_DIMENSION} dims, got {unit.shape[1]}")
        return unit.astype(np.float32).tolist()
