"""Loading and validation of the editable memory (TOML and JSON Lines)."""
import json
import re
import tomllib
from pathlib import Path

from . import SCHEMA_VERSION
from .prefs import PrefConflict, resolve
from .rules import OPS, walk
from .util import parse_date

PREDICATES = {
    "downloaded", "read", "commented_on", "offered_to_help_with", "requested_revision",
    "received_revision", "corresponded_with", "asked_not_to_contact",
}
USES = {"plan", "mention"}
SLOTS = ["subject", "greeting", "reason", "context", "request", "support", "closing", "signoff"]
REQUIRED_SLOTS = ["subject", "greeting", "reason", "context", "request", "closing", "signoff"]
KINDS = {
    "recipient_offered_help", "recipient_commented", "recipient_requested_revision", "recipient_downloaded",
    "recipient_read_passage", "prior_exchange", "project_older_essay", "project_in_progress",
    "project_revision", "project_argument", "project_url",
}
PLACEHOLDERS = {
    "name", "title", "canonical_title", "url", "defect", "argument", "revision_note",
    "offer_topic", "comment_topic", "locator", "author",
}
PH_ASPECT = {
    "title": "title", "canonical_title": "title", "url": "url", "defect": "defect_kinds",
    "argument": "argument", "revision_note": "revision_note",
}
PH_KIND = {
    "offer_topic": "recipient_offered_help", "comment_topic": "recipient_commented",
    "locator": "recipient_commented", "revision_note": "project_revision",
    "argument": "project_argument", "url": "project_url",
}
REFERENTS = {"project"}
PRONOUN = re.compile(r"\b(it|its|this essay|this monograph|the essay|the monograph|the draft|the new version)\b", re.I)
PH_RE = re.compile(r"\{(\w+)\}")
RULE_DEFAULTS = {
    "purposes": ["*"], "register": ["plain", "warm"], "directness": ["direct"], "asserts": [], "implies": [],
    "introduces": [], "needs": [], "follows": [], "language": "en", "canonical": False,
    "repeatable": False, "construction": "statement",
}


def read_toml(path):
    with open(path, "rb") as f:
        return tomllib.load(f)


def read_jsonl(path):
    out = []
    with open(path, encoding="utf-8") as f:
        for n, line in enumerate(f, 1):
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError as exc:
                    raise ValueError(f"{path}:{n}: {exc}") from exc
    return out


def normalize_rule(r):
    r = dict(r)
    for k, v in RULE_DEFAULTS.items():
        r.setdefault(k, list(v) if isinstance(v, list) else v)
    r.setdefault("move", r.get("slot"))
    if r.get("slot") == "request":
        r.setdefault("requests", 1)
    else:
        r.setdefault("requests", 0)
    r["placeholders"] = sorted(set(PH_RE.findall(r.get("text", ""))))
    r["aspects"] = sorted({PH_ASPECT[p] for p in r["placeholders"] if p in PH_ASPECT})
    derived = {PH_KIND[p] for p in r["placeholders"] if p in PH_KIND}
    r["needs_kinds"] = sorted(derived | set(r["asserts"]) | set(r["implies"]))
    return r


class Memory:
    pass


def load_memory(root):
    root = Path(root)
    m = Memory()
    m.root = root
    raw = {}
    raw["author"] = read_toml(root / "author.toml")["author"]
    raw["project"] = read_toml(root / "projects.toml").get("project", [])
    raw["goal"] = read_toml(root / "goals.toml").get("goal", [])
    raw["occasion"] = read_toml(root / "occasions.toml").get("occasion", [])
    raw["pref"] = read_toml(root / "preferences.toml").get("pref", [])
    raw["inference"] = read_toml(root / "inferences.toml").get("inference", [])
    raw["recipient"] = read_jsonl(root / "recipients.jsonl")
    raw["evidence"] = read_jsonl(root / "evidence.jsonl")
    raw["slot"] = read_toml(root / "slots.toml").get("slot", [])
    rules, triggers = [], []
    for f in sorted((root / "grammar").glob("*.toml")):
        data = read_toml(f)
        for r in data.get("rule", []):
            rr = normalize_rule(r)
            rr["file"] = f.name
            rules.append(rr)
        triggers.extend(data.get("trigger", []))
    raw["rule"] = rules
    raw["trigger"] = triggers
    m.raw = raw
    m.author = raw["author"]
    m.projects = {p["id"]: p for p in raw["project"]}
    m.goals = list(raw["goal"])
    m.occasions = {o["id"]: o for o in raw["occasion"]}
    m.prefs = list(raw["pref"])
    m.inferences = list(raw["inference"])
    m.recipients = {r["id"]: r for r in raw["recipient"]}
    m.evidence = {}
    for e in raw["evidence"]:
        m.evidence.setdefault(e["subject"], []).append(e)
    for lst in m.evidence.values():
        lst.sort(key=lambda e: (e["observed"], e["id"]))
    m.slots = sorted(raw["slot"], key=lambda s: s["id"])
    m.rules = sorted(rules, key=lambda r: r["id"])
    m.triggers = triggers
    return m


def validate(m):
    """Return a list of error strings. An empty list means the memory is valid."""
    errs = []
    raw = m.raw

    def need(kind, rec, keys):
        for k in keys:
            if k not in rec:
                errs.append(f"{kind} {rec.get('id', '?')}: missing field {k!r}")

    def dups(kind, items):
        seen = set()
        for it in items:
            i = it.get("id")
            if i in seen:
                errs.append(f"duplicate {kind} id {i!r}")
            seen.add(i)

    for kind in ("project", "goal", "occasion", "pref", "inference", "recipient", "evidence", "slot", "rule"):
        dups(kind, raw[kind])
    dups("trigger", raw["trigger"])

    for p in raw["project"]:
        need("project", p, ["id", "title", "short_title", "url", "status", "kind"])
    for g in raw["goal"]:
        need("goal", g, ["id", "purpose", "projects", "request", "requires", "priority", "active_from",
                         "active_to", "foreground"])
        for pid in g.get("projects", []):
            if pid not in m.projects:
                errs.append(f"goal {g['id']}: unknown project {pid!r}")
        for key in ("requires", "disqualify"):
            if key in g:
                for op, arg in walk(g[key]):
                    if op not in OPS:
                        errs.append(f"goal {g['id']}: unknown operation {op!r}")
                    elif op in ("has", "missing"):
                        if arg.get("predicate") not in PREDICATES:
                            errs.append(f"goal {g['id']}: predicate {arg.get('predicate')!r} cannot be expressed by the memory schema")
                        if arg.get("project") not in (None, "any") and not str(arg["project"]).startswith("$") \
                                and arg["project"] not in m.projects:
                            errs.append(f"goal {g['id']}: unknown project {arg['project']!r} in condition")
        for d in ("active_from", "active_to"):
            try:
                parse_date(g[d])
            except Exception:
                errs.append(f"goal {g['id']}: bad date in {d}")
    for o in raw["occasion"]:
        need("occasion", o, ["id", "allowed_purposes"])
    purposes = {g["purpose"] for g in raw["goal"]}
    for o in raw["occasion"]:
        for p in o.get("allowed_purposes", []):
            if p not in purposes:
                errs.append(f"occasion {o['id']}: unknown purpose {p!r}")
    for r in raw["recipient"]:
        need("recipient", r, ["id", "display_name", "language"])
    for e in raw["evidence"]:
        need("evidence", e, ["id", "subject", "predicate", "observed", "uses"])
        if e.get("subject") not in m.recipients:
            errs.append(f"evidence {e.get('id')}: unknown subject {e.get('subject')!r}")
        if e.get("predicate") not in PREDICATES:
            errs.append(f"evidence {e.get('id')}: unknown predicate {e.get('predicate')!r}")
        if e.get("project") and e["project"] not in m.projects:
            errs.append(f"evidence {e.get('id')}: unknown project {e['project']!r}")
        for u in e.get("uses", []):
            if u not in USES:
                errs.append(f"evidence {e.get('id')}: unknown use {u!r}")
        try:
            parse_date(e["observed"])
        except Exception:
            errs.append(f"evidence {e.get('id')}: bad observed date")
    for s in raw["slot"]:
        need("slot", s, ["id", "mode", "recipient", "occasion"])
        if s.get("recipient") not in m.recipients:
            errs.append(f"slot {s['id']}: unknown recipient {s.get('recipient')!r}")
        if s.get("occasion") not in m.occasions:
            errs.append(f"slot {s['id']}: unknown occasion {s.get('occasion')!r}")
        if s.get("project") and s["project"] not in m.projects:
            errs.append(f"slot {s['id']}: unknown project {s['project']!r}")
        if s.get("purpose") and s["purpose"] not in purposes:
            errs.append(f"slot {s['id']}: unknown purpose {s['purpose']!r}")
    for inf in raw["inference"]:
        for k in ("from", "to"):
            if inf.get(k) not in KINDS:
                errs.append(f"inference {inf.get('id')}: unknown kind {inf.get(k)!r}")
    for p in raw["pref"]:
        need("pref", p, ["id", "scope", "key", "value"])
        sc = p.get("scope", "")
        if sc != "global":
            kind, _, ident = sc.partition(":")
            if kind == "project" and ident not in m.projects:
                errs.append(f"pref {p['id']}: unknown project scope {ident!r}")
            elif kind == "occasion" and ident not in m.occasions:
                errs.append(f"pref {p['id']}: unknown occasion scope {ident!r}")
            elif kind not in ("project", "occasion"):
                errs.append(f"pref {p['id']}: bad scope {sc!r}")
    # preference conflicts across every occasion and project combination
    try:
        ref = parse_date("2026-11-11")
        for occ in list(m.occasions) + [None]:
            for proj in list(m.projects) + [None]:
                resolve(m.prefs, occ, proj, ref)
                # also check both ends of every effective window
                for p in m.prefs:
                    for edge in ("effective_from", "effective_to"):
                        if p.get(edge):
                            resolve(m.prefs, occ, proj, parse_date(p[edge]))
    except PrefConflict as exc:
        errs.append(f"preference conflict: {exc}")
    except Exception as exc:
        errs.append(f"preference error: {exc}")

    # grammar
    for t in raw["trigger"]:
        try:
            re.compile(t["pattern"])
        except re.error as exc:
            errs.append(f"trigger {t.get('id')}: bad pattern ({exc})")
        if t.get("kind") not in KINDS:
            errs.append(f"trigger {t.get('id')}: unknown kind {t.get('kind')!r}")
    for r in raw["rule"]:
        rid = r.get("id")
        if r.get("slot") not in SLOTS:
            errs.append(f"rule {rid}: unknown slot {r.get('slot')!r}")
            continue
        if not r.get("text"):
            errs.append(f"rule {rid}: no text")
            continue
        for ph in r["placeholders"]:
            if ph not in PLACEHOLDERS:
                errs.append(f"rule {rid}: unknown placeholder {{{ph}}}")
        for k in r["asserts"] + r["implies"]:
            if k not in KINDS:
                errs.append(f"rule {rid}: unknown assertion kind {k!r}")
        for ref_name in r["needs"] + r["introduces"]:
            if ref_name not in REFERENTS:
                errs.append(f"rule {rid}: unknown referent {ref_name!r}")
        for pu in r["purposes"]:
            if pu != "*" and pu not in purposes:
                errs.append(f"rule {rid}: unknown purpose {pu!r}")
        if r["slot"] == "request":
            for k in ("action", "obligation"):
                if k not in r:
                    errs.append(f"rule {rid}: request rule needs {k!r}")
            if r["requests"] != 1:
                errs.append(f"rule {rid}: request rule must count exactly one request")
        elif r["requests"] != 0:
            errs.append(f"rule {rid}: only request rules may count a request")
        if PRONOUN.search(r["text"]) and "project" not in r["needs"] and "{title}" not in r["text"] \
                and "{canonical_title}" not in r["text"]:
            errs.append(f"rule {rid}: pronoun or definite reference without a recoverable referent")
        if r["slot"] == "subject" and (r["needs"] or r["introduces"]):
            errs.append(f"rule {rid}: subject rules cannot use referents")
    for g in raw["goal"]:
        for slot in REQUIRED_SLOTS:
            usable = [r for r in raw["rule"] if r.get("slot") == slot
                      and ("*" in r["purposes"] or g["purpose"] in r["purposes"])
                      and (slot != "request" or r.get("action") == g["request"])]
            if not usable:
                errs.append(f"goal {g['id']}: no grammar rule for slot {slot!r}")
            elif not any(r["canonical"] for r in usable):
                errs.append(f"goal {g['id']}: no canonical rule for slot {slot!r}")
    return errs


def schema_info():
    return {"schema_version": SCHEMA_VERSION, "predicates": sorted(PREDICATES), "kinds": sorted(KINDS)}
