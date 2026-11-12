"""Symbolic planning: eligibility, scoring, selection and scope-carrying certificates."""
from .prefs import resolve as resolve_prefs, values as pref_values
from .rules import Ctx, evaluate
from .util import canon, h64, parse_date, pick, sha256_hex

SCORE_VERSION = "score-v1"
STRENGTH = {"commented_on": 40, "requested_revision": 35, "offered_to_help_with": 30, "read": 20, "downloaded": 5}
INTEREST = {"downloaded", "read", "commented_on", "requested_revision"}
KIND_SPEC = {
    "recipient_offered_help": ("offered_to_help_with", "any"),
    "recipient_commented": ("commented_on", "project"),
    "recipient_requested_revision": ("requested_revision", "project"),
    "recipient_downloaded": ("downloaded", "project"),
    "prior_exchange": ("corresponded_with", "any"),
}


def _in_window(goal, ref):
    return parse_date(goal["active_from"]) <= ref <= parse_date(goal["active_to"])


class Planner:
    def __init__(self, mem, seed, ref_date, band=0, languages=None):
        self.mem = mem
        self.seed = seed
        self.ref = ref_date
        self.band = band
        self.languages = languages

    # evaluation of every (goal, project) pair
    def evaluate_all(self, slot):
        mem = self.mem
        rec = mem.recipients[slot["recipient"]]
        occ = mem.occasions[slot["occasion"]]
        evs = mem.evidence.get(rec["id"], [])
        out = []
        for goal in mem.goals:
            if goal["purpose"] not in occ["allowed_purposes"]:
                continue
            if slot.get("purpose") and goal["purpose"] != slot["purpose"]:
                continue
            for pid in goal["projects"]:
                if slot.get("project") and pid != slot["project"]:
                    continue
                proj = mem.projects[pid]
                c = {"goal": goal["id"], "purpose": goal["purpose"], "project": pid, "active": _in_window(goal, self.ref)}
                if not c["active"]:
                    c.update(eligible=False, requires=None, disqualified=None, why="goal_inactive", score=None)
                    out.append(c)
                    continue
                ctx = Ctx(evs, proj, goal, self.ref)
                req = evaluate(goal["requires"], ctx)
                req_trace = ctx.trace
                ctx.trace = []
                dis = evaluate(goal["disqualify"], ctx) if "disqualify" in goal else False
                dis_trace = ctx.trace
                c.update(requires=req, disqualified=dis, requires_trace=req_trace, disqualify_trace=dis_trace,
                         binds={k: v["id"] for k, v in ctx.binds.items()})
                c["eligible"] = req is True and dis is not True
                c["unknown_disqualifier"] = dis is None
                if not c["eligible"]:
                    c["why"] = "disqualified" if (req is True and dis is True) else (
                        "requirement_unknown" if req is None else "requirement_failed")
                    c["score"] = None
                else:
                    c["score"], c["score_parts"] = self._score(goal, pid, ctx.binds, evs)
                    c["_binds"] = ctx.binds
                out.append(c)
        return out

    def _score(self, goal, pid, binds, evs):
        bound = list(binds.values())
        if any(b.get("project") == pid for b in bound):
            relevance = 40
        elif any(e["predicate"] in INTEREST and e.get("project") == pid and "plan" in e["uses"] for e in evs):
            relevance = 25
        else:
            relevance = 0
        continuity = 20 if any(e["predicate"] == "corresponded_with" and "plan" in e["uses"] for e in evs) else 0
        strength = max([STRENGTH.get(b["predicate"], 0) for b in bound] or [0])
        recent = any(0 <= (self.ref - parse_date(b["observed"])).days <= 365 for b in bound)
        if recent:
            strength += 10
        parts = {"relevance": relevance, "continuity": continuity, "evidence_strength": strength,
                 "author_priority": int(goal.get("priority", 0)), "version": SCORE_VERSION}
        return relevance + continuity + strength + parts["author_priority"], parts

    def abstain_reason(self, cands, occ):
        if not cands:
            return "no_admissible_goal"
        if any(c.get("why") == "disqualified" for c in cands):
            return "recipient_excluded"
        if all(not c["active"] for c in cands):
            return "no_active_goal"
        if occ.get("require_recipient_reason"):
            return "no_supported_recipient_context"
        return "no_eligible_plan"

    def plan(self, slot, debug=False):
        mem = self.mem
        rec = mem.recipients[slot["recipient"]]
        occ = mem.occasions[slot["occasion"]]
        base = {"slot": slot["id"], "recipient": rec["id"], "occasion": occ["id"]}
        if self.languages is not None and rec["language"] not in self.languages:
            return dict(base, status="abstained", reason="unsupported_language")
        cands = self.evaluate_all(slot)
        elig = [c for c in cands if c["eligible"]]
        trace = None
        if debug:
            trace = [{k: v for k, v in c.items() if not k.startswith("_")} for c in cands]
        if not elig:
            out = dict(base, status="abstained", reason=self.abstain_reason(cands, occ))
            if debug:
                out["evaluation"] = trace
            return out
        elig.sort(key=lambda c: (-c["score"], c["goal"], c["project"]))
        top = elig[0]["score"]
        band = [c for c in elig if c["score"] >= top - self.band]
        chosen = band[h64(self.seed, slot["id"], "plan", "tie") % len(band)] if len(band) > 1 else band[0]
        plan = self._build_plan(slot, rec, occ, chosen)
        out = dict(base, status="planned", reason=None, plan=plan)
        if debug:
            out["evaluation"] = trace
            out["ranking"] = [{"goal": c["goal"], "project": c["project"], "score": c["score"]} for c in elig]
        return out

    # plan construction with certificates
    def _build_plan(self, slot, rec, occ, chosen):
        mem = self.mem
        goal = next(g for g in mem.goals if g["id"] == chosen["goal"])
        proj = mem.projects[chosen["project"]]
        evs = mem.evidence.get(rec["id"], [])
        certs = self._certificates(rec, occ, goal, proj, evs, chosen["_binds"])
        prefs = resolve_prefs(mem.prefs, occ["id"], proj["id"], self.ref)
        constraints = pref_values(prefs)
        style = constraints.get("title_style", "canonical")
        title = proj["short_title"] if style == "short" else proj["title"]
        aspects = proj.get("aspects", {})
        fields = {
            "name": rec.get("given_name") or rec["display_name"],
            "title": title,
            "canonical_title": proj["title"],
            "url": proj["url"],
            "author": mem.author["name"],
        }
        if aspects.get("defect_kinds"):
            fields["defect"] = pick(aspects["defect_kinds"], self.seed, slot["id"], "defect")
        if aspects.get("argument"):
            fields["argument"] = aspects["argument"]
        if aspects.get("revision_note") and proj.get("revision"):
            fields["revision_note"] = aspects["revision_note"]
        for c in certs:
            if c["use"] == "mention":
                fields.update({k: v for k, v in c["fields"].items() if v is not None})
        mention_kinds = sorted({c["kind"] for c in certs if c["use"] == "mention"})
        evidence_kinds = sorted({k for k in mention_kinds if not k.startswith("project_")})
        plan = {
            "slot": slot["id"],
            "recipient": rec["id"],
            "language": rec["language"],
            "occasion": occ["id"],
            "purpose": goal["purpose"],
            "goal": goal["id"],
            "project": proj["id"],
            "subject_intent": "state_task",
            "opening_move": "cite_recipient_evidence" if evidence_kinds else "introduce_task",
            "core_claim": {"goal": goal["id"], "project": proj["id"]},
            "request": {"action": goal["request"], "obligation": "ask_if_able"},
            "closing_intent": "scope_reply",
            "aspects": {
                "foreground": list(goal.get("foreground", [])),
                "optional": list(goal.get("optional", [])),
                "excluded": list(goal.get("excluded", [])),
            },
            "fields": fields,
            "certificates": certs,
            "mention_kinds": mention_kinds,
            "constraints": constraints,
            "constraint_sources": {k: v["source"] for k, v in prefs.items()},
            "score": chosen["score"],
            "score_parts": chosen["score_parts"],
        }
        plan["plan_sig"] = self.plan_signature(plan)
        plan["plan_hash"] = sha256_hex(canon({k: v for k, v in plan.items() if k != "plan_hash"}))[:24]
        return plan

    @staticmethod
    def plan_signature(plan):
        key = "|".join([
            plan["purpose"], plan["project"], plan["request"]["action"], plan["opening_move"],
            ",".join(sorted(k for k in plan["mention_kinds"] if not k.startswith("project_"))),
            ",".join(sorted(plan["aspects"]["foreground"])), plan["fields"].get("defect", "-"),
        ])
        return sha256_hex(key)[:12]

    def _certificates(self, rec, occ, goal, proj, evs, binds):
        certs = []
        n = 0
        scope = {"recipient": rec["id"], "occasion": occ["id"], "purpose": goal["purpose"], "project": proj["id"]}

        def add(kind, prop, sources, rule, use, fields):
            nonlocal n
            n += 1
            certs.append({"id": f"c{n}", "kind": kind, "proposition": prop, "sources": sources, "rule": rule,
                          "scope": scope, "use": use, "fields": fields})

        for kind, (pred, pscope) in sorted(KIND_SPEC.items()):
            pool = [e for e in evs if e["predicate"] == pred and not e.get("negated") and "plan" in e["uses"]
                    and (pscope == "any" or e.get("project") == proj["id"])]
            if not pool:
                continue
            e = sorted(pool, key=lambda x: (x["observed"], x["id"]))[-1]
            use = "mention" if "mention" in e["uses"] else "plan_only"
            if kind == "recipient_offered_help":
                prop, flds = f"{rec['id']} offered to help with {e['value']}", {"offer_topic": e["value"]}
            elif kind == "recipient_commented":
                prop = f"{rec['id']} commented on {e['value']}" + (f" at {e['locator']}" if e.get("locator") else "")
                flds = {"comment_topic": e["value"], "locator": e.get("locator")}
            elif kind == "recipient_requested_revision":
                prop, flds = f"{rec['id']} asked to see a revised version of {proj['id']}", {}
            elif kind == "recipient_downloaded":
                prop, flds = f"{rec['id']} downloaded {proj['id']}", {}
            else:
                prop, flds = f"{rec['id']} has an existing exchange with the author", {}
            add(kind, prop, [e["id"]], "direct", use, flds)
        for inf in self.mem.inferences:
            src = next((c for c in certs if c["kind"] == inf["from"]), None)
            if src is None:
                continue
            if inf.get("requires_locator") and not src["fields"].get("locator"):
                continue
            add(inf["to"], src["proposition"].replace("commented on", "read the passage of"), src["sources"],
                inf["id"], src["use"], {})
        if proj["kind"] == "essay" and proj.get("age") == "older":
            add("project_older_essay", f"{proj['id']} is an older essay", [f"project:{proj['id']}"], "direct", "mention", {})
        if proj.get("status") == "revising":
            add("project_in_progress", f"{proj['id']} is under revision", [f"project:{proj['id']}"], "direct", "mention", {})
        if proj.get("revision") and proj.get("revised"):
            add("project_revision", f"{proj['id']} has revision {proj['revision']}", [f"project:{proj['id']}"],
                "direct", "mention", {"revision_note": proj.get("aspects", {}).get("revision_note")})
        if proj.get("aspects", {}).get("argument"):
            add("project_argument", f"{proj['id']} has a stated central argument", [f"project:{proj['id']}"], "direct",
                "mention", {})
        add("project_url", f"{proj['id']} is available at its canonical URL", [f"project:{proj['id']}"], "direct", "mention", {})
        return certs
