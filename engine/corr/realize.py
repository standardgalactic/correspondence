"""Surface realization from typed clause plans.

A candidate is identified by an id of the form order|subject|slot rules... where the
rule ids follow the slot order of the chosen discourse order. A candidate is built
from the id alone, so any draft can be reconstructed from its rule path and plan.
"""
import re

from .util import fingerprint, h64, pick, sha256_hex

ORDERS = {
    "reason_first": ["greeting", "reason", "context", "request", "support", "closing", "signoff"],
    "request_first": ["greeting", "request", "reason", "support", "closing", "signoff"],
    "context_first": ["greeting", "context", "reason", "request", "support", "closing", "signoff"],
}
ORDER_NAMES = sorted(ORDERS)
EMOJI = re.compile("[\U0001F300-\U0001FAFF☀-➿⭐⬆️]")
HOLE = re.compile(r"\{\w*\}")
PH = re.compile(r"\{(\w+)\}")


class Realizer:
    def __init__(self, mem):
        self.rules = {r["id"]: r for r in mem.rules}
        self.by_slot = {}
        for r in mem.rules:
            self.by_slot.setdefault(r["slot"], []).append(r)
        self.triggers = [(re.compile(t["pattern"], re.I), t["kind"]) for t in mem.triggers]
        self.languages = {r["language"] for r in mem.rules}

    # eligibility of rules for one plan
    def eligible(self, plan):
        c = plan["constraints"]
        reg = set(c.get("register", ["plain"]))
        direct = set(c.get("directness", ["direct"]))
        allowed = set(plan["aspects"]["foreground"]) | set(plan["aspects"]["optional"])
        excluded = set(plan["aspects"]["excluded"])
        kinds = set(plan["mention_kinds"])
        fields = plan["fields"]
        out = {}
        for slot, rules in self.by_slot.items():
            keep = []
            for r in rules:
                if "*" not in r["purposes"] and plan["purpose"] not in r["purposes"]:
                    continue
                if r["language"] != plan["language"]:
                    continue
                if not reg & set(r["register"]) or not direct & set(r["directness"]):
                    continue
                if not set(r["needs_kinds"]) <= kinds:
                    continue
                if not set(r["placeholders"]) <= set(fields):
                    continue
                used = set(r["aspects"])
                if used & excluded or not used <= allowed:
                    continue
                if slot == "request" and r["action"] != plan["request"]["action"]:
                    continue
                keep.append(r)
            out[slot] = sorted(keep, key=lambda r: r["id"])
        return out

    def sample_ids(self, plan, seed, k, elig):
        for slot in ("subject", "greeting", "reason", "context", "request", "closing", "signoff"):
            if not elig.get(slot):
                return [], f"no_rule_for_slot:{slot}"
        sid = plan["slot"]
        ids = set()
        for i in range(k):
            order = pick(ORDER_NAMES, seed, sid, "render", "order", i)
            parts = [order, pick(elig["subject"], seed, sid, "render", "subject", i)["id"]]
            for slot in ORDERS[order]:
                opts = elig[slot] if slot != "support" else [None] + elig["support"]
                if not opts:
                    parts.append("-")
                    continue
                r = pick(opts, seed, sid, "render", slot, i)
                parts.append(r["id"] if r else "-")
            ids.add("|".join(parts))
        return sorted(ids), None

    def canonical_id(self, plan, elig):
        parts = ["reason_first"]
        for slot in ["subject"] + ORDERS["reason_first"]:
            if slot == "support":
                parts.append("-")
                continue
            canon = [r for r in elig.get(slot, []) if r["canonical"]]
            if not canon:
                return None
            parts.append(canon[0]["id"])
        return "|".join(parts)

    # building and validation of a single candidate
    def build(self, plan, cid, elig=None):
        elig = elig or self.eligible(plan)
        parts = cid.split("|")
        order, sub_id, rest = parts[0], parts[1], parts[2:]
        if order not in ORDERS or len(rest) != len(ORDERS[order]):
            return {"ok": False, "error": "malformed_candidate_id", "id": cid}
        fields = plan["fields"]
        c = plan["constraints"]
        kinds = set(plan["mention_kinds"])
        eligible_ids = {r["id"] for rs in elig.values() for r in rs}

        def fill(text):
            return PH.sub(lambda m: str(fields.get(m.group(1), m.group(0))), text)

        def fail(err):
            return {"ok": False, "error": err, "id": cid}

        sub_rule = self.rules.get(sub_id)
        if sub_rule is None or sub_rule["slot"] != "subject" or sub_id not in eligible_ids:
            return fail("ineligible_rule:" + sub_id)
        subject = fill(sub_rule["text"])
        used = [sub_rule]
        pieces = []
        intro, moves, prev = set(), [], "start"
        requests = 0
        for slot, rid in zip(ORDERS[order], rest):
            if rid == "-":
                if slot != "support":
                    return fail(f"missing_required_slot:{slot}")
                continue
            r = self.rules.get(rid)
            if r is None or r["slot"] != slot or rid not in eligible_ids:
                return fail("ineligible_rule:" + rid)
            if r["follows"] and prev not in r["follows"]:
                return fail(f"discourse_order:{rid}")
            if not set(r["needs"]) <= intro:
                return fail(f"unresolved_referent:{rid}")
            intro |= set(r["introduces"])
            if r["move"] in moves and not r["repeatable"]:
                return fail(f"repeated_move:{r['move']}")
            moves.append(r["move"])
            requests += r["requests"]
            if slot == "request":
                if r["action"] != plan["request"]["action"] or r["obligation"] != plan["request"]["obligation"]:
                    return fail(f"request_changed:{rid}")
            pieces.append((slot, fill(r["text"])))
            used.append(r)
            prev = slot
        for r in used:
            if not set(r["needs_kinds"]) <= kinds:
                return fail(f"unsupported_implication:{r['id']}")
        if sum(1 for r in used[1:] if "url" in r["aspects"]) > 1:
            return fail("repeated_aspect:url")
        if requests != 1 or requests > c.get("max_requests", 1):
            return fail(f"request_count:{requests}")
        paras, cur = [], []

        def flush():
            nonlocal cur
            if cur:
                paras.append(" ".join(cur))
                cur = []

        for slot, text in pieces:
            if slot == "greeting":
                paras.append(text)
            elif slot == "signoff":
                flush()
                paras.append(text)
            else:
                if slot in ("request", "closing"):
                    flush()
                cur.append(text)
        body = "\n\n".join(paras)
        whole = subject + "\n" + body
        if HOLE.search(whole):
            return fail("unresolved_slot")
        for pat, kind in self.triggers:
            if pat.search(whole) and kind not in kinds:
                return fail(f"unsupported_implication:{kind}")
        low = whole.lower()
        for ch in c.get("forbid_chars", []):
            if ch in whole:
                return fail("forbidden_character")
        for ph in c.get("forbid_phrases", []):
            if ph.lower() in low:
                return fail("forbidden_phrase")
        if c.get("forbid_emoji") and EMOJI.search(whole):
            return fail("emoji")
        words = len(body.split())
        if words > c.get("max_words", 10 ** 6):
            return fail("too_long")
        if len(subject) > 90:
            return fail("subject_too_long")
        if body.count(fields["title"]) > c.get("title_max_mentions", 10 ** 6):
            return fail("title_repeated")
        for asp in plan["aspects"]["excluded"]:
            for val in self._aspect_values(plan, asp):
                if val and val.lower() in low:
                    return fail(f"excluded_aspect:{asp}")
        construction = ".".join(r["construction"] for r in used)
        sig = sha256_hex(order + ":" + construction)[:12]
        return {"ok": True, "id": cid, "subject": subject, "body": body, "words": words,
                "fp": fingerprint(subject, body), "sig": sig, "rules": [r["id"] for r in used], "order": order,
                "construction": construction}

    @staticmethod
    def _aspect_values(plan, aspect):
        f = plan["fields"]
        if aspect == "argument":
            return [f.get("argument")]
        if aspect == "revision_note":
            return [f.get("revision_note")]
        if aspect == "defect_kinds":
            return [f.get("defect")]
        return []

    def candidates(self, plan, seed, k, debug=False):
        elig = self.eligible(plan)
        ids, err = self.sample_ids(plan, seed, k, elig)
        valid, rejected = [], []
        for cid in ids:
            b = self.build(plan, cid, elig)
            (valid if b["ok"] else rejected).append(b)
        return valid, rejected, err, elig

    @staticmethod
    def rank(seed, slot_id, cid):
        return h64(seed, slot_id, "select", cid)
