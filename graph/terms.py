r"""The word index behind vague prompts: every node's name and description as word stems, so a prompt that names
nothing exactly ("why did the sitting thing break?") still finds "Post-process AnimBP swap crash". No model
(CLAUDE.md rule 1): a small stemmer, a short synonym list (vocabulary/synonyms.json) and rare-word weighting.

store.py keeps the `terms` table current on every upsert; recall.py asks fuzzy_seeds() when it seeds a prompt.
"""
import json
import math
import re
from pathlib import Path

SYNONYMS_FILE = Path(__file__).resolve().parents[1] / "vocabulary" / "synonyms.json"
MIN_WORD = 3
MIN_PREFIX = 4
NAME, DESC = 0, 1
# Words that say nothing about what a prompt is about. Short words (< 3 letters) are dropped anyway.
STOP = {
    "the", "and", "for", "with", "this", "that", "what", "why", "how", "when", "where", "who", "was", "are", "you",
    "can", "did", "does", "not", "but", "from", "into", "then", "there", "its", "our", "your", "about", "again",
    "also", "any", "all", "been", "being", "could", "should", "would", "will", "have", "has", "had", "were", "they",
    "them", "their", "which", "some", "one", "now", "just", "still", "thing", "things", "stuff", "something", "get",
    "got", "make", "made", "use", "used", "like", "want", "need", "know", "think", "last", "back", "time", "way",
    "remember", "recall", "earlier", "before", "after", "other", "same", "more", "most", "very", "really", "only",
    "each", "every", "here", "than", "too", "out", "off", "over", "let", "lets", "please", "yes", "okay", "ok",
}


def words(text: str) -> list:
    """Lowercase words, with camelCase and snake_case split: 'AnimBP_Player' -> ['anim', 'bp', 'player']."""
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", text or "")
    text = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", text)
    return re.findall(r"[a-z0-9]+", text.lower())


def stem(w: str) -> str:
    """A light suffix stripper: crashes/crashed/crashing -> crash, sitting -> sit, override/overriding -> overrid.
    Only consistency matters: the same word family must end up the same on both sides."""
    for suf, keep in (("ies", "y"), ("sses", "ss"), ("ing", ""), ("ed", ""), ("es", ""), ("s", ""), ("ly", "")):
        if w.endswith(suf) and len(w) - len(suf) >= 3 and not (suf == "s" and w.endswith("ss")):
            w = w[: len(w) - len(suf)] + keep
            if suf in ("ing", "ed") and len(w) >= 4 and w[-1] == w[-2] and w[-1] not in "lsz":
                w = w[:-1]  # sitting -> sitt -> sit
            break
    return w[:-1] if len(w) > 4 and w.endswith("e") else w


def _load_synonyms() -> dict:
    try:
        groups = json.loads(SYNONYMS_FILE.read_text(encoding="utf-8"))["groups"]
    except (OSError, ValueError, KeyError):
        return {}
    out = {}
    for g in groups:
        stems = [stem(w.lower()) for w in g if isinstance(w, str)]
        for s in stems:
            out.setdefault(s, stems[0])
    return out


SYNONYMS = _load_synonyms()


def terms_of(text: str) -> set:
    return {SYNONYMS.get(s, s) for s in (stem(w) for w in words(text) if len(w) >= MIN_WORD and w not in STOP
                                         and not w.isdigit())}


def index_entity(db, eid: str, name: str, description: str) -> None:
    db.execute("DELETE FROM terms WHERE entity_id=?", (eid,))
    rows = {(t, eid, NAME) for t in terms_of(name)} | {(t, eid, DESC) for t in terms_of(description)}
    db.executemany("INSERT OR IGNORE INTO terms(term, entity_id, field) VALUES (?,?,?)", sorted(rows))


def rebuild(db) -> None:
    db.execute("DELETE FROM terms")
    for eid, name, desc in db.execute("SELECT id, name, description FROM entities").fetchall():
        index_entity(db, eid, name, desc)
    db.commit()


# Scoring: each prompt term adds its rarity (idf) to the nodes it hits, full for a name word, half for a description
# word, less again for a prefix ("anim" ~ "animation"). A node needs two different prompt words, or one rare name word.
FIELD_WEIGHT = {NAME: 1.0, DESC: 0.5}
PREFIX_WEIGHT = 0.7
RARE_SINGLE = 2  # a single word hits at most this many nodes to seed alone
KEEP_RATIO = 0.5  # seeds score at least half the best one


def fuzzy_seeds(db, text: str, limit: int = 3, exclude: set | None = None) -> dict:
    """{entity_id: score} of the nodes the prompt's words point at best, highest first; {} when nothing stands out."""
    prompt = terms_of(text)
    if not prompt:
        return {}
    total = db.execute("SELECT COUNT(*) FROM entities").fetchone()[0] or 1
    score, hits, single = {}, {}, {}
    for t in prompt:
        rows = [(e, f, 1.0) for e, f in db.execute("SELECT entity_id, field FROM terms WHERE term=?", (t,))]
        if len(t) >= MIN_PREFIX:  # index words that start with this one, and this one's own prefixes
            rows += [(e, f, PREFIX_WEIGHT) for e, f in db.execute(
                "SELECT entity_id, field FROM terms WHERE term > ? AND term < ?", (t, t + "{"))]
            short = [t[:k] for k in range(MIN_PREFIX, len(t))]
            if short:
                marks = ",".join("?" * len(short))
                rows += [(e, f, PREFIX_WEIGHT) for e, f in db.execute(
                    f"SELECT entity_id, field FROM terms WHERE term IN ({marks})", short)]  # noqa: S608
        if not rows:
            continue
        df = len({e for e, _, _ in rows})
        idf = math.log(1 + total / df)
        best = {}
        for e, f, w in rows:
            best[e] = max(best.get(e, 0.0), FIELD_WEIGHT[f] * w)
        for e, w in best.items():
            score[e] = score.get(e, 0.0) + idf * w
            hits[e] = hits.get(e, 0) + 1
            if df <= RARE_SINGLE and w == 1.0:
                single[e] = True
    exclude = exclude or set()
    ok = {e: s for e, s in score.items() if e not in exclude and (hits[e] >= 2 or single.get(e))}
    if not ok:
        return {}
    top = max(ok.values())
    ranked = sorted(((s, e) for e, s in ok.items() if s >= top * KEEP_RATIO), reverse=True)[:limit]
    return {e: s for s, e in ranked}
