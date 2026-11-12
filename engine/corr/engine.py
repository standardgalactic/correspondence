"""Glue between planner and realizer. run_slot is a pure function of snapshot, seed, date and slot."""
from .planner import Planner
from .realize import Realizer


class Engine:
    def __init__(self, mem, seed, ref_date, k=8, band=0):
        self.mem = mem
        self.seed = seed
        self.k = k
        self.realizer = Realizer(mem)
        self.planner = Planner(mem, seed, ref_date, band, languages=self.realizer.languages)

    def run_slot(self, slot, debug=False):
        out = self.planner.plan(slot, debug)
        if out["status"] != "planned":
            return out
        plan = out["plan"]
        valid, rejected, err, elig = self.realizer.candidates(plan, self.seed, self.k)
        out["fallback"] = None
        if not valid:
            cid = self.realizer.canonical_id(plan, elig)
            built = self.realizer.build(plan, cid, elig) if cid else None
            if built and built["ok"]:
                valid = [built]
                out["fallback"] = "canonical"
            else:
                out["status"] = "failed"
                out["reason"] = "rendering_failure:" + (err or (built or {}).get("error") or "no_canonical_rule")
        out["candidates"] = valid
        if debug:
            out["rejected"] = rejected
        return out

    def select(self, plan_slot_id, cands):
        """Candidates in ascending selection rank (stable and independent of how many are valid)."""
        return sorted(cands, key=lambda c: (self.realizer.rank(self.seed, plan_slot_id, c["id"]), c["id"]))

    def render(self, plan, cid):
        return self.realizer.build(plan, cid)
