"""Turn stored examples into honest train / calibration / test splits and feature matrices.

Honesty rules:
  - near-duplicates (MinHash over word shingles) are kept in the same split, so a test score
    is never inflated by a paraphrase of a training example
  - splits are stratified by label at the group level
  - human-verified gold examples, when present, are the test set and never train
"""
import re
import zlib
from collections import Counter, defaultdict

import numpy as np
from scipy import sparse

from .common import FAMILIES, devdecide, facts_for, make_item, read_jsonl

MIN_GOLD_TEST = 20
SPLIT = {"train": 0.70, "calib": 0.15, "test": 0.15}
NUM_PERM, BANDS = 32, 8
PRIME = 4294967311
NEAR_DUP = 0.6


class Sample(object):
    __slots__ = ("ex", "label", "weight", "gold", "group", "split", "item", "facts", "vec")

    def __init__(self, ex, label, weight, gold):
        self.ex, self.label, self.weight, self.gold = ex, label, weight, gold
        self.group = self.split = self.item = self.facts = None
        self.vec = {}


def load(paths, family):
    examples = read_jsonl(paths.examples(family))
    gold = {}
    for g in read_jsonl(paths.gold(family)):
        gold.setdefault(g["id"], {}).update(g.get("labels", {}))
    return examples, gold


def usable(examples, gold, task):
    """Samples for one task: human labels first, otherwise the agreed teacher label."""
    out = []
    for ex in examples:
        g = gold.get(ex["id"], {})
        if task in g:
            out.append(Sample(ex, str(g[task]), 1.0, True))
        elif task in ex.get("labels", {}):
            conf = (ex.get("teacher", {}).get(task) or [None, 0.7])[1]
            out.append(Sample(ex, str(ex["labels"][task]), min(max(float(conf), 0.3), 1.0), False))
    return out


# ---------------------------------------------------------------- near-duplicates

def _dedup_text(ex):
    inp = ex["input"]
    if inp.get("kind") == "diff":
        lines = [l[1:] for l in inp.get("diff", "").splitlines()
                 if l[:1] in "+-" and not l.startswith(("+++", "---"))]
        return " ".join(lines)
    return inp.get("text", "")


def _shingles(text, n):
    toks = re.findall(r"[a-z0-9]+", text.lower())
    if len(toks) < n:
        grams = [" ".join(toks)] if toks else [text.lower()[:50]]
    else:
        grams = [" ".join(toks[i:i + n]) for i in range(len(toks) - n + 1)]
    hashes = sorted(set(zlib.crc32(g.encode("utf-8")) for g in grams))
    return np.array(hashes[:400], dtype=np.uint64)  # bottom-400 keeps long diffs cheap


def near_dup_groups(examples, family):
    n_gram = 3 if FAMILIES[family]["input"] == "diff" else 2
    rng = np.random.RandomState(12345)
    a = rng.randint(1, 2 ** 31 - 1, size=NUM_PERM).astype(np.uint64)
    b = rng.randint(0, 2 ** 31 - 1, size=NUM_PERM).astype(np.uint64)
    sigs = np.empty((len(examples), NUM_PERM), dtype=np.uint64)
    for i, ex in enumerate(examples):
        h = _shingles(_dedup_text(ex), n_gram)
        sigs[i] = ((a[:, None] * h[None, :] + b[:, None]) % PRIME).min(axis=1)
    parent = list(range(len(examples)))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    rows = NUM_PERM // BANDS
    for band in range(BANDS):
        buckets = defaultdict(list)
        block = sigs[:, band * rows:(band + 1) * rows]
        for i in range(len(examples)):
            buckets[block[i].tobytes()].append(i)
        for members in buckets.values():
            first = members[0]
            for j in members[1:]:
                if (sigs[first] == sigs[j]).mean() >= NEAR_DUP:
                    ra, rb = find(first), find(j)
                    if ra != rb:
                        parent[rb] = ra
    return [find(i) for i in range(len(examples))]


# ---------------------------------------------------------------- splits

def _order_key(group, seed):
    return zlib.crc32(("%s|%s" % (seed, group)).encode())


def assign_splits(samples, seed=7):
    """Group-aware, label-stratified splits. Gold samples are the test set when there are enough."""
    gold_n = sum(1 for s in samples if s.gold)
    use_gold = gold_n >= MIN_GOLD_TEST
    gold_groups = set(s.group for s in samples if s.gold) if use_gold else set()
    by_group = defaultdict(list)
    for s in samples:
        by_group[s.group].append(s)
    groups_by_label = defaultdict(list)
    for g, members in by_group.items():
        if g in gold_groups:
            for s in members:  # non-gold near-duplicates of gold items would leak: drop them
                s.split = "test" if s.gold else "drop"
            continue
        majority = Counter(s.label for s in members).most_common(1)[0][0]
        groups_by_label[majority].append(g)
    for label, groups in groups_by_label.items():
        groups.sort(key=lambda g: _order_key(g, seed))
        n = len(groups)
        if use_gold:
            n_test, n_cal = 0, (max(1, int(round(0.15 * n))) if n >= 2 else 0)
        elif n >= 3:
            n_test = max(1, int(round(SPLIT["test"] * n)))
            n_cal = max(1, int(round(SPLIT["calib"] * n)))
        elif n == 2:
            n_test, n_cal = 1, 0
        else:
            n_test = n_cal = 0
        for i, g in enumerate(groups):
            split = "test" if i < n_test else "calib" if i < n_test + n_cal else "train"
            for s in by_group[g]:
                s.split = split
    return use_gold


# ---------------------------------------------------------------- features

def featurize(samples, names=("f1", "f2")):
    D = devdecide()
    for s in samples:
        if s.item is None:
            s.item = make_item(s.ex)
            s.facts = facts_for(s.item)
        for name in names:
            if name not in s.vec:
                s.vec[name] = D["fm"].FEATURIZERS[name](s.item, s.facts)


def matrices(samples, name):
    """CSR matrices per split over the buckets seen in training (unseen buckets have no weight at inference)."""
    train = [s for s in samples if s.split == "train"]
    cols = {}
    for s in train:
        for k in s.vec[name]:
            if k not in cols:
                cols[k] = len(cols)

    def build(subset):
        data, idx, ptr = [], [], [0]
        for s in subset:
            for k, v in s.vec[name].items():
                c = cols.get(k)
                if c is not None:
                    idx.append(c)
                    data.append(v)
            ptr.append(len(idx))
        return sparse.csr_matrix((np.array(data, dtype=np.float64), np.array(idx, dtype=np.int64),
                                  np.array(ptr, dtype=np.int64)), shape=(len(subset), max(1, len(cols))))

    out = {}
    for split in ("train", "calib", "test"):
        subset = [s for s in samples if s.split == split]
        out[split] = (subset, build(subset))
    inv = np.empty(len(cols), dtype=np.int64)
    for k, c in cols.items():
        inv[c] = k
    return out, inv
