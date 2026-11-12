import datetime
import hashlib
import json
import re
import unicodedata

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def h64(*parts):
    """Stable 64 bit hash of the given parts."""
    data = "\x1f".join(str(p) for p in parts).encode("utf-8")
    return int.from_bytes(hashlib.sha256(data).digest()[:8], "big")


def pick(seq, *parts):
    return seq[h64(*parts) % len(seq)]


def canon(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_hex(data):
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def norm_text(text):
    """Documented normalization: NFC, recipient addresses removed, whitespace collapsed."""
    text = unicodedata.normalize("NFC", text)
    text = EMAIL_RE.sub("", text)
    return re.sub(r"\s+", " ", text).strip()


def fingerprint(subject, body):
    return sha256_hex(norm_text(subject) + "\n" + norm_text(body))[:32]


def parse_date(value):
    if isinstance(value, datetime.date):
        return value
    return datetime.date.fromisoformat(str(value))
