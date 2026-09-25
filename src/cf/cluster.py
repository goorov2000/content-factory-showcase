"""Кластеризация «other» по капшенам/транскриптам: TF-IDF + жадная группировка.

700 сырых строк в контекст агента не загружаются: агент получает компактные
кластеры с примерами и только называет их.
"""
import math
import re
from collections import Counter

STOPWORDS = set("""и в на не с по для как это что мы вы он она они а но или же
у о от до из за то так вот бы ли к the a of to in and for is are on with
""".split())
TOKEN_RE = re.compile(r"[a-zа-яё]{3,}", re.IGNORECASE)


def tokenize(text):
    text = re.sub(r"https?://\S+|#\S+|@\S+", " ", str(text or "").lower())
    return [t for t in TOKEN_RE.findall(text) if t not in STOPWORDS]


class _Vector:
    """Разреженный TF-IDF-вектор с предвычисленной евклидовой нормой.

    Норма считается ОДИН раз при построении вектора, а не заново на каждой из
    O(n²) пар в жадной кластеризации (P3.8): для строки i её норма иначе
    пересчитывалась бы сотни раз по всем термам. Теперь косинус пары —
    это только скалярное произведение по общим термам, делённое на две
    готовые нормы.
    """

    __slots__ = ("weights", "norm")

    def __init__(self, weights):
        self.weights = weights
        self.norm = math.sqrt(sum(v * v for v in weights.values()))

    def __bool__(self):
        return bool(self.weights)


def _vector(row, idf):
    counts = Counter(tokenize(f'{row.get("caption", "")} {row.get("transcript_text", "")}'))
    return _Vector({t: n * idf.get(t, 0.0) for t, n in counts.items()})


def _cosine(a, b):
    common = set(a.weights) & set(b.weights)
    num = sum(a.weights[t] * b.weights[t] for t in common)
    den = a.norm * b.norm
    return num / den if den else 0.0


def cluster_rows(rows, min_size=30, threshold=0.25, samples=5):
    docs = [tokenize(f'{r.get("caption", "")} {r.get("transcript_text", "")}') for r in rows]
    df = Counter(t for d in docs for t in set(d))
    idf = {t: math.log(len(rows) / n) for t, n in df.items()}
    vectors = [_vector(r, idf) for r in rows]
    assigned = [False] * len(rows)
    clusters = []
    for i, vec in enumerate(vectors):
        if assigned[i] or not vec:
            continue
        members = [i]
        assigned[i] = True
        for j in range(i + 1, len(rows)):
            if not assigned[j] and _cosine(vec, vectors[j]) >= threshold:
                members.append(j)
                assigned[j] = True
        if len(members) >= min_size:
            terms = Counter(t for m in members for t in docs[m])
            clusters.append({
                "size": len(members),
                "top_terms": [t for t, _ in terms.most_common(8)],
                "sample_rows": [{"source_url": rows[m].get("source_url", ""),
                                 "caption": str(rows[m].get("caption", ""))[:160]}
                                for m in members[:samples]],
                "source_urls": [rows[m].get("source_url", "") for m in members],
            })
    return sorted(clusters, key=lambda c: -c["size"])
