"""Meaning features: a small static sentence-embedding model, read with the Python standard library.

The bundled model is potion-base-8M by Minish Lab (MIT license), distilled from BAAI/bge-base-en-v1.5. It turns
a text into 256 numbers that place similar meanings close together, so "refund my order" and "I want my money
back" share evidence even though they share no words. Encoding is a WordPiece tokenization followed by an average
of token vectors. This module reproduces model2vec's StaticModel.encode (checked against the reference library;
see the 5.0 notes in README.md), without numpy or the tokenizers package.

The weight file is memory-mapped: a new process reads only the rows of the tokens it sees. Texts are encoded once
per process (LRU cache).
"""
import functools
import json
import mmap
import os
import re
import statistics
import struct
import sys
import threading
import unicodedata
from array import array
from pathlib import Path

MODEL_NAME = "potion-base-8M"
SIDECAR = "dev-decide.json"   # tokenizer settings and the median token length, written on first use
VOCAB = "vocab.txt"           # tokens in id order: faster to read than tokenizer.json
_WHITESPACE = set("\t\n\r              "
                  "    　")
_CJK = ((0x4E00, 0x9FFF), (0x3400, 0x4DBF), (0x20000, 0x2A6DF), (0x2A700, 0x2B73F), (0x2B740, 0x2B81F),
        (0x2B820, 0x2CEAF), (0xF900, 0xFAFF), (0x2F800, 0x2FA1F))
_OTHER = ("Cc", "Cf", "Co", "Cs")  # Hugging Face's "other" check does not remove unassigned code points (Cn)
_LOCK = threading.Lock()
_MODELS = {}


def locate():
    """The model folder, or None: DEV_DECIDE_SEMANTIC, the one bundled with the skill, then ~/.claude/dev-decide."""
    here = Path(__file__).resolve().parent.parent
    cands = []
    if os.environ.get("DEV_DECIDE_SEMANTIC"):
        cands.append(Path(os.environ["DEV_DECIDE_SEMANTIC"]))
    cands += [here / "semantic" / MODEL_NAME, Path(os.path.expanduser("~")) / ".claude" / "dev-decide" / "semantic" / MODEL_NAME]
    for c in cands:
        if (c / "model.safetensors").is_file() and ((c / SIDECAR).is_file() or (c / "tokenizer.json").is_file()):
            return c
    return None


def get(path=None):
    """The loaded model (cached per process), or None when no model is installed."""
    path = Path(path) if path else locate()
    if path is None:
        return None
    key = str(path)
    with _LOCK:
        m = _MODELS.get(key)
        if m is None:
            try:
                m = Model(path)
            except Exception:
                m = False
            _MODELS[key] = m
    return m or None


def available():
    return get() is not None


def _is_cjk(cp):
    return any(lo <= cp <= hi for lo, hi in _CJK)


def _is_punct(c):
    cp = ord(c)
    if 33 <= cp <= 47 or 58 <= cp <= 64 or 91 <= cp <= 96 or 123 <= cp <= 126:
        return True
    return unicodedata.category(c).startswith("P")


def prepare(path):
    """Write the sidecar and vocab files next to tokenizer.json (done once, or by the packaging step)."""
    path = Path(path)
    tok = json.loads((path / "tokenizer.json").read_text(encoding="utf-8"))
    cfg = json.loads((path / "config.json").read_text(encoding="utf-8")) if (path / "config.json").is_file() else {}
    model, norm, pre = tok["model"], tok.get("normalizer") or {}, tok.get("pre_tokenizer") or {}
    if model.get("type") != "WordPiece" or norm.get("type") != "BertNormalizer" or pre.get("type") != "BertPreTokenizer":
        raise ValueError("unsupported tokenizer: only BERT-style WordPiece models are supported")
    vocab = model["vocab"]
    tokens = sorted(vocab, key=vocab.get)
    if [vocab[t] for t in tokens] != list(range(len(tokens))):
        raise ValueError("vocabulary ids are not contiguous")
    lowercase = bool(norm.get("lowercase", True))
    strip = norm.get("strip_accents")
    side = {
        "v": 1, "unk": model.get("unk_token", "[UNK]"), "prefix": model.get("continuing_subword_prefix", "##"),
        "max_chars": int(model.get("max_input_chars_per_word", 100)), "lowercase": lowercase,
        "strip_accents": lowercase if strip is None else bool(strip), "clean_text": bool(norm.get("clean_text", True)),
        "chinese": bool(norm.get("handle_chinese_chars", True)),
        "specials": [a["content"] for a in tok.get("added_tokens") or [] if a.get("special")],
        "median_token_length": int(statistics.median(len(t) for t in tokens)),
        "max_length": int(cfg.get("max_length") or 512), "normalize": bool(cfg.get("normalize", True)),
        "vocab_size": len(tokens),
    }
    if any("\n" in t for t in tokens):
        raise ValueError("a token contains a newline")
    (path / VOCAB).write_text("\n".join(tokens), encoding="utf-8")
    (path / SIDECAR).write_text(json.dumps(side, indent=1), encoding="utf-8")
    return side


class Model(object):
    def __init__(self, path):
        self.path = Path(path)
        if not (self.path / SIDECAR).is_file() or not (self.path / VOCAB).is_file():
            prepare(self.path)
        side = json.loads((self.path / SIDECAR).read_text(encoding="utf-8"))
        tokens = (self.path / VOCAB).read_text(encoding="utf-8").split("\n")
        if len(tokens) != side["vocab_size"]:
            raise ValueError("vocab.txt does not match the sidecar")
        self.vocab = dict(zip(tokens, range(len(tokens))))
        self.unk = self.vocab[side["unk"]]
        self.prefix = side["prefix"]
        self.max_chars = side["max_chars"]
        self.lowercase, self.strip, self.clean, self.chinese = (side["lowercase"], side["strip_accents"],
                                                                side["clean_text"], side["chinese"])
        self.max_length = side["max_length"]
        self.max_input = side["max_length"] * side["median_token_length"]
        self.normalize_out = side["normalize"]
        specials = sorted((s for s in side["specials"] if s in self.vocab), key=len, reverse=True)
        self.specials = re.compile("|".join(re.escape(s) for s in specials)) if specials else None
        with open(str(self.path / "model.safetensors"), "rb") as fh:
            n = struct.unpack("<Q", fh.read(8))[0]
            header = json.loads(fh.read(n).decode("utf-8"))
            self._mm = mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ)
        info = header.get("embeddings") or next(v for k, v in header.items() if k != "__metadata__")
        if info["dtype"] != "F32" or len(info["shape"]) != 2 or info["shape"][0] != len(tokens):
            raise ValueError("expected a float32 [vocab, dim] embedding matrix")
        self.dim = int(info["shape"][1])
        start, end = info["data_offsets"]
        self._base = 8 + n + start
        if end - start != 4 * self.dim * len(tokens):
            raise ValueError("embedding matrix size does not match its shape")
        self._words = {}
        self.encode = functools.lru_cache(maxsize=4096)(self._encode)

    # -- tokenization (BertNormalizer, BertPreTokenizer, WordPiece), as in Hugging Face tokenizers
    def _normalize(self, s):
        out = []
        for c in s:
            cp = ord(c)
            if self.clean:
                if cp == 0 or cp == 0xFFFD or (c not in "\t\n\r" and unicodedata.category(c) in _OTHER):
                    continue
                if c in _WHITESPACE:
                    out.append(" ")
                    continue
            if self.chinese and cp >= 0x3400 and _is_cjk(cp):
                out.append(" %s " % c)
                continue
            out.append(c)
        s = "".join(out)
        if self.strip:
            s = "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")
        if self.lowercase:
            s = "".join(c.lower() for c in s)  # per character, like Rust's char::to_lowercase
        return s

    def _wordpiece(self, word):
        hit = self._words.get(word)
        if hit is not None:
            return hit
        vocab, n = self.vocab, len(word)
        if n > self.max_chars:
            ids = (self.unk,)
        else:
            ids, start = [], 0
            while start < n:
                end, cur = n, None
                while start < end:
                    sub = word[start:end] if start == 0 else self.prefix + word[start:end]
                    cur = vocab.get(sub)
                    if cur is not None:
                        break
                    end -= 1
                if cur is None:
                    ids = [self.unk]
                    break
                ids.append(cur)
                start = end
            ids = tuple(ids)
        if len(self._words) < 200000:
            self._words[word] = ids
        return ids

    def _plain(self, text, out):
        for piece in self._normalize(text).split():
            word = []
            for c in piece:
                if _is_punct(c):
                    if word:
                        out.extend(self._wordpiece("".join(word)))
                        word = []
                    out.extend(self._wordpiece(c))
                else:
                    word.append(c)
            if word:
                out.extend(self._wordpiece("".join(word)))

    def tokenize(self, text):
        text = (text or "")[:self.max_input]
        ids = []
        if self.specials is None:
            self._plain(text, ids)
        else:
            pos = 0
            for m in self.specials.finditer(text):
                self._plain(text[pos:m.start()], ids)
                ids.append(self.vocab[m.group(0)])
                pos = m.end()
            self._plain(text[pos:], ids)
        ids = ids[:self.max_length]
        return [i for i in ids if i != self.unk]

    def row(self, i):
        a = array("f")
        o = self._base + 4 * self.dim * i
        a.frombytes(self._mm[o:o + 4 * self.dim])
        if sys.byteorder != "little":
            a.byteswap()
        return a

    def _encode(self, text):
        """The text's normalized embedding as a tuple of floats, or None when it has no known tokens."""
        ids = self.tokenize(text)
        if not ids:
            return None
        counts = {}
        for i in ids:
            counts[i] = counts.get(i, 0) + 1
        acc = [0.0] * self.dim
        for i, c in counts.items():
            r = self.row(i)
            if c == 1:
                acc = [a + b for a, b in zip(acc, r)]
            else:
                acc = [a + c * b for a, b in zip(acc, r)]
        n = float(len(ids))
        acc = [a / n for a in acc]
        if self.normalize_out:
            norm = sum(a * a for a in acc) ** 0.5 + 1e-32
            acc = [a / norm for a in acc]
        return tuple(acc)
