"""Train the two classifiers the scorer blends, from the public data in data/ only.

  python3 train/train.py [--out models] [--report]

  synthetic  data/synthetic_train.jsonl  2000 short, LLM-written chat prompts labelled haiku|sonnet|opus
  measured   data/swe_labeled.jsonl      150 SWE-bench issues, labelled with the cheapest model that solved them
                                         (Haiku -> Sonnet -> Opus cascade; unsolved issues are labelled opus)

Both are TF-IDF (word + character n-grams) into logistic regression, so training takes seconds
and nothing leaves your machine. --report prints held-out accuracy.
"""
import argparse
import json
import os
import pickle
import random
from collections import Counter

from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import FeatureUnion, Pipeline

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")


def rows(name):
    with open(os.path.join(DATA, name), encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def synthetic_model():
    feats = FeatureUnion([
        ("word", TfidfVectorizer(ngram_range=(1, 2), min_df=2, sublinear_tf=True)),
        ("char", TfidfVectorizer(analyzer="char_wb", ngram_range=(2, 5), min_df=3, sublinear_tf=True)),
    ])
    return Pipeline([("f", feats), ("clf", LogisticRegression(max_iter=2000, C=4.0))])


def measured_model():
    feats = FeatureUnion([
        ("word", TfidfVectorizer(ngram_range=(1, 2), min_df=2, sublinear_tf=True, max_features=20000)),
        ("char", TfidfVectorizer(analyzer="char_wb", ngram_range=(2, 5), min_df=2, sublinear_tf=True, max_features=30000)),
    ])
    return Pipeline([("f", feats), ("clf", LogisticRegression(C=4, max_iter=2000, class_weight="balanced"))])


def fit(make, data):
    m = make()
    m.fit([r["text"] for r in data], [r["label"] for r in data])
    return m


def report(name, make, data):
    data = data[:]
    random.Random(0).shuffle(data)
    cut = int(len(data) * 0.8)
    train, test = data[:cut], data[cut:]
    m = fit(make, train)
    preds = m.predict([r["text"] for r in test])
    truth = [r["label"] for r in test]
    acc = sum(p == t for p, t in zip(preds, truth)) / len(truth)
    base = Counter(truth).most_common(1)[0][1] / len(truth)
    print(f"{name}: held-out accuracy {acc:.0%} on {len(test)} (largest-class baseline {base:.0%})")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(ROOT, "models"))
    ap.add_argument("--report", action="store_true")
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)
    for name, make, src in (("synthetic", synthetic_model, "synthetic_train.jsonl"),
                            ("measured", measured_model, "swe_labeled.jsonl")):
        data = rows(src) + (rows("design_train.jsonl") if name == "synthetic" else [])
        if args.report:
            report(name, make, data)
        with open(os.path.join(args.out, f"{name}.pkl"), "wb") as f:
            pickle.dump(fit(make, data), f)
        print(f"trained {name} on {len(data)} rows {dict(Counter(r['label'] for r in data))}")


if __name__ == "__main__":
    main()
