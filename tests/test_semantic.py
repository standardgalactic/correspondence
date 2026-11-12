"""Semantic acceptance tests for the correspondence generator (section 8 of the specification)."""
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "engine"))

from corr import batch as B  # noqa: E402
from corr.engine import Engine  # noqa: E402
from corr.memory import load_memory, validate  # noqa: E402
from corr.realize import ORDERS  # noqa: E402
from corr.rules import Ctx, evaluate  # noqa: E402
from corr.store import unpack  # noqa: E402
from corr.util import parse_date  # noqa: E402

SEED = 20261111
REF = parse_date("2026-11-11")


def slot(rid, occasion="outreach", **kw):
    d = {"id": f"t-{rid}", "mode": "experiment", "recipient": rid, "occasion": occasion}
    d.update(kw)
    return d


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def memdir(self):
        d = self.tmp / "memory"
        shutil.copytree(ROOT / "memory", d)
        return d

    def engine(self, memdir=None, seed=SEED, k=12):
        return Engine(load_memory(memdir or ROOT / "memory"), seed, REF, k)

    def candidates(self, rid, occasion="outreach", seeds=range(25), memdir=None, **kw):
        """All valid candidates for a recipient over several seeds, as (engine, plan, candidate)."""
        mem = load_memory(memdir or ROOT / "memory")
        out = []
        for s in seeds:
            e = Engine(mem, s, REF, 12)
            r = e.run_slot(slot(rid, occasion, **kw))
            if r["status"] == "planned":
                out += [(e, r["plan"], c) for c in r["candidates"]]
        return out


class TestEvaluator(Base):
    def test_absence_is_unknown_not_false(self):
        ctx = Ctx([], {"id": "p"}, {}, REF)
        self.assertIsNone(evaluate({"has": {"predicate": "commented_on"}}, ctx))
        self.assertTrue(evaluate({"missing": {"predicate": "commented_on"}}, ctx))
        self.assertIsNone(evaluate({"all": [{"has": {"predicate": "commented_on"}}, {"eq": [1, 1]}]}, ctx))
        self.assertFalse(evaluate({"all": [{"has": {"predicate": "commented_on"}}, {"eq": [1, 2]}]}, ctx))

    def test_explicit_negation_is_false(self):
        ev = [{"id": "e1", "predicate": "read", "uses": ["plan"], "observed": "2026-01-01", "negated": True}]
        ctx = Ctx(ev, {"id": "p"}, {}, REF)
        self.assertFalse(evaluate({"has": {"predicate": "read"}}, ctx))


class TestValidation(Base):
    def test_memory_is_valid(self):
        self.assertEqual(validate(load_memory(ROOT / "memory")), [])

    def test_hard_preference_conflict_is_an_error(self):
        d = self.memdir()
        with open(d / "preferences.toml", "a") as f:
            f.write('\n[[pref]]\nid = "x"\nscope = "occasion:outreach"\nkey = "max_requests"\nvalue = 2\nhard = true\n')
        errs = validate(load_memory(d))
        self.assertTrue(any("hard constraints disagree" in e for e in errs), errs)

    def test_equal_authority_soft_conflict_is_an_error(self):
        d = self.memdir()
        with open(d / "preferences.toml", "a") as f:
            f.write('\n[[pref]]\nid = "x"\nscope = "global"\nkey = "title_style"\nvalue = "short"\npriority = 10\n')
        errs = validate(load_memory(d))
        self.assertTrue(any("equal authority" in e for e in errs), errs)

    def test_broken_reference_and_unexpressible_predicate(self):
        d = self.memdir()
        txt = (d / "goals.toml").read_text().replace('predicate = "commented_on"', 'predicate = "praised"')
        (d / "goals.toml").write_text(txt)
        with open(d / "evidence.jsonl", "a") as f:
            f.write(json.dumps({"id": "e-x", "subject": "r-nobody", "predicate": "read", "observed": "2026-01-01", "uses": ["plan"]}) + "\n")
        errs = validate(load_memory(d))
        self.assertTrue(any("cannot be expressed" in e for e in errs))
        self.assertTrue(any("unknown subject" in e for e in errs))

    def test_pronoun_without_referent_is_rejected(self):
        d = self.memdir()
        with open(d / "grammar" / "extra.toml", "w") as f:
            f.write('rule = [ { id = "x-bad", slot = "closing", text = "Thank you for reading it." } ]\n')
        errs = validate(load_memory(d))
        self.assertTrue(any("x-bad" in e and "referent" in e for e in errs))


class TestPlanning(Base):
    def test_removing_required_evidence_makes_plan_ineligible(self):
        base = self.engine().run_slot(slot("r-ben"))
        self.assertEqual(base["status"], "planned")
        self.assertEqual(base["plan"]["goal"], "substantive-followup")
        d = self.memdir()
        lines = [ln for ln in (d / "evidence.jsonl").read_text().splitlines() if '"e-ben-1"' not in ln]
        (d / "evidence.jsonl").write_text("\n".join(lines) + "\n")
        out = self.engine(d).run_slot(slot("r-ben"))
        self.assertEqual((out["status"], out["reason"]), ("abstained", "no_supported_recipient_context"))

    def test_irrelevant_change_leaves_plan_alone(self):
        before = self.engine().run_slot(slot("r-ben"))
        d = self.memdir()
        txt = (d / "evidence.jsonl").read_text().replace('"e-ada-1", "subject": "r-ada"', '"e-ada-1", "subject": "r-ada"').replace(
            '"value": "PDF", "source": "download log", "observed": "2026-09-14"', '"value": "EPUB", "source": "mail", "observed": "2026-09-15"')
        (d / "evidence.jsonl").write_text(txt)
        g = (d / "projects.toml").read_text().replace("stray spacing in displayed formulas", "ragged margins")
        (d / "projects.toml").write_text(g)
        after = self.engine(d).run_slot(slot("r-ben"))
        self.assertEqual(before["plan"], after["plan"])
        self.assertEqual([c["id"] for c in before["candidates"]], [c["id"] for c in after["candidates"]])

    def test_discrimination_between_similar_recipients(self):
        e = self.engine()
        expect = {
            "r-ada": ("abstained", "no_supported_recipient_context"),
            "r-jo": ("abstained", "no_supported_recipient_context"),
            "r-fay": ("abstained", "no_supported_recipient_context"),
            "r-kim": ("abstained", "recipient_excluded"),
            "r-ivan": ("abstained", "unsupported_language"),
        }
        for rid, (st, why) in expect.items():
            out = e.run_slot(slot(rid))
            self.assertEqual((out["status"], out["reason"]), (st, why), rid)
        goals = {rid: e.run_slot(slot(rid))["plan"]["goal"] for rid in
                 ("r-ben", "r-chloe", "r-dev", "r-eli", "r-gus", "r-hana", "r-lee")}
        self.assertEqual(goals["r-chloe"], "typeset-correction")
        self.assertEqual(goals["r-eli"], "share-revision")
        self.assertEqual(goals["r-lee"], "substantive-followup")
        self.assertEqual(e.run_slot(slot("r-dev"))["plan"]["project"], "relief-of-explanation")

    def test_purpose_constraint_selects_lower_ranked_plan(self):
        out = self.engine().run_slot(slot("r-lee", purpose="typesetting_correction"))
        self.assertEqual(out["plan"]["goal"], "typeset-correction")

    def test_expired_goal_abstains(self):
        d = self.memdir()
        txt = (d / "goals.toml").read_text().replace('active_to = "2027-06-30"', 'active_to = "2026-10-31"')
        (d / "goals.toml").write_text(txt)
        out = self.engine(d).run_slot(slot("r-ben"))
        self.assertEqual((out["status"], out["reason"]), ("abstained", "no_active_goal"))

    def test_revision_date_boundary(self):
        d = self.memdir()
        txt = (d / "projects.toml").read_text().replace('revised = "2026-11-08"', 'revised = "2026-11-10"')
        (d / "projects.toml").write_text(txt)
        e = self.engine(d)
        self.assertEqual(e.run_slot(slot("r-fay"))["status"], "planned")
        self.assertEqual(e.run_slot(slot("r-eli"))["status"], "planned")

    def test_already_received_revision_disqualifies(self):
        d = self.memdir()
        with open(d / "evidence.jsonl", "a") as f:
            f.write(json.dumps({"id": "e-eli-9", "subject": "r-eli", "predicate": "received_revision", "project": "proof-abundance",
                                "value": "v2", "observed": "2026-11-09", "uses": ["plan"]}) + "\n")
        out = self.engine(d).run_slot(slot("r-eli", "followup"))
        self.assertEqual((out["status"], out["reason"]), ("abstained", "recipient_excluded"))

    def test_preference_layers(self):
        plan = self.engine().run_slot(slot("r-ben"))["plan"]
        self.assertEqual(plan["fields"]["title"], "Proof Abundance")
        self.assertEqual(plan["constraints"]["max_words"], 150)
        plan = self.engine().run_slot(slot("r-hana", "followup"))["plan"]
        self.assertEqual(plan["constraints"]["max_words"], 120)
        self.assertEqual(plan["constraints"]["title_max_mentions"], 1)
        self.assertEqual(plan["constraints"]["max_requests"], 1)


class TestRealization(Base):
    RECIPIENTS = ["r-ben", "r-chloe", "r-dev", "r-eli", "r-gus", "r-hana", "r-lee"]

    def test_variation_preserves_action_and_assertions(self):
        for rid in self.RECIPIENTS:
            occ = "followup" if rid in ("r-eli", "r-hana") else "outreach"
            cands = self.candidates(rid, occ, seeds=range(12))
            self.assertTrue(cands, rid)
            for e, plan, c in cands:
                rules = [e.realizer.rules[r] for r in c["rules"]]
                reqs = [r for r in rules if r["slot"] == "request"]
                self.assertEqual(len(reqs), 1)
                self.assertEqual(reqs[0]["action"], plan["request"]["action"])
                self.assertEqual(reqs[0]["obligation"], plan["request"]["obligation"])
                for r in rules:
                    self.assertTrue(set(r["needs_kinds"]) <= set(plan["mention_kinds"]), (r["id"], rid))

    def test_unsupported_prior_exchange_wording_fails(self):
        d = self.memdir()
        with open(d / "grammar" / "extra.toml", "w") as f:
            f.write('rule = [ { id = "x-again", slot = "closing", construction = "statement", text = "Thanks again for everything." } ]\n')
        for rid, occ, expect_ok in (("r-ben", "outreach", False), ("r-hana", "followup", True)):
            e = self.engine(d)
            r = e.run_slot(slot(rid, occ))
            c = r["candidates"][0]
            parts = c["id"].split("|")
            order = parts[0]
            idx = 2 + ORDERS[order].index("closing")
            parts[idx] = "x-again"
            built = e.realizer.build(r["plan"], "|".join(parts))
            self.assertEqual(built["ok"], expect_ok, (rid, built.get("error")))
            if not expect_ok:
                self.assertIn("unsupported_implication", built["error"])
        for e, plan, c in self.candidates("r-ben", memdir=d):
            self.assertNotIn("x-again", c["rules"])
            self.assertNotIn("again", c["body"].lower().split())

    def test_unknown_and_plan_only_facts_never_appear(self):
        mem = load_memory(ROOT / "memory")
        for e, plan, c in self.candidates("r-gus"):
            low = (c["subject"] + c["body"]).lower()
            for needle in ("treatment of attribution", "page 31", "your comment", "you commented"):
                self.assertNotIn(needle, low)
            self.assertNotIn("recipient_commented", plan["mention_kinds"])
        for e, plan, c in self.candidates("r-dev"):
            self.assertNotIn("you have read", c["body"].lower())
        self.assertEqual(self.engine().run_slot(slot("r-ada"))["status"], "abstained")

    def test_downloaded_evidence_supports_only_downloaded_claims(self):
        seen = [c for e, plan, c in self.candidates("r-dev") if "t-r-dl" in c["rules"]]
        self.assertTrue(seen)
        for e, plan, c in self.candidates("r-chloe"):
            self.assertNotIn("a copy of", c["body"])

    def test_excluded_aspects_stay_out_of_text(self):
        mem = load_memory(ROOT / "memory")
        args = [p["aspects"]["argument"].lower() for p in mem.projects.values() if "argument" in p.get("aspects", {})]
        for rid in ("r-chloe", "r-dev"):
            for e, plan, c in self.candidates(rid):
                for a in args:
                    self.assertNotIn(a, (c["subject"] + c["body"]).lower())
        for e, plan, c in self.candidates("r-eli", "followup"):
            self.assertNotIn(plan["fields"]["defect"], c["body"])

    def test_title_once_regression(self):
        for rid in ("r-hana", "r-eli"):
            for e, plan, c in self.candidates(rid, "followup"):
                self.assertLessEqual(c["body"].count(plan["fields"]["title"]), 1)
                self.assertEqual(sum(1 for r in c["rules"] if e.realizer.rules[r]["slot"] == "request"), 1)

    def test_hard_constraints_hold_everywhere(self):
        for rid in self.RECIPIENTS:
            occ = "followup" if rid in ("r-eli", "r-hana") else "outreach"
            for e, plan, c in self.candidates(rid, occ, seeds=range(6)):
                whole = c["subject"] + c["body"]
                self.assertNotIn("\u2014", whole)
                self.assertNotIn("{", whole)
                self.assertLessEqual(c["words"], plan["constraints"]["max_words"])
                self.assertNotIn("hope this email finds you well", whole.lower())

    def test_prior_exchange_wording_requires_evidence(self):
        wording = ("following up", "thanks again", "earlier exchange")
        for e, plan, c in self.candidates("r-ben"):
            self.assertFalse(any(w in c["body"].lower() for w in wording))
        self.assertTrue(any(any(w in c["body"].lower() for w in wording) for e, p, c in self.candidates("r-hana", "followup")))

    def test_every_draft_traces_to_rules_and_rebuilds(self):
        for e, plan, c in self.candidates("r-lee", seeds=range(5)):
            again = e.render(plan, c["id"])
            self.assertTrue(again["ok"])
            self.assertEqual(again["fp"], c["fp"])


class TestBatches(Base):
    def make_snapshot(self, memdir=None, name="snap"):
        out = self.tmp / name
        B.freeze(memdir or ROOT / "memory", out, "2026-11-11")
        return out

    def manifest(self, snap, mode="experiment", distinct=True, trials=40, name="m.toml", bid="t"):
        scen = '["r-ada", "r-ben", "r-chloe", "r-eli", "r-gus", "r-hana", "r-lee"]'
        body = f'''[batch]
id = "{bid}"
mode = "{mode}"
snapshot = "{snap}"
seed = 7
reference_date = "2026-11-11"
candidates_per_slot = 10
require_distinct = {"true" if distinct else "false"}

[slots]
occasion = "outreach"
trials = {trials}
scenarios = {scen}
'''
        p = self.tmp / name
        p.write_text(body)
        return p

    def test_identical_results_across_workers_and_interruption(self):
        snap = self.make_snapshot()
        m = self.manifest(snap)
        s1 = B.generate(m, workers=1, out_root=self.tmp / "r1", chunk=25)
        s1.close()
        s2 = B.generate(m, workers=2, out_root=self.tmp / "r2", chunk=25)
        s2.close()
        with self.assertRaises(B.Interrupted):
            B.generate(m, workers=2, out_root=self.tmp / "r3", chunk=25, stop_after_chunks=3)
        with self.assertRaises(B.Interrupted):
            B.generate(m, workers=1, out_root=self.tmp / "r3", chunk=25, stop_after_slots=60)
        s3 = B.generate(m, workers=2, out_root=self.tmp / "r3", chunk=25)
        s3.close()
        a1, a2, a3 = (B.audit(self.tmp / r / "t") for r in ("r1", "r2", "r3"))
        self.assertTrue(a1["complete"] and a1["all_slots_accounted"])
        self.assertEqual(a1["output_hash"], a2["output_hash"])
        self.assertEqual(a1["output_hash"], a3["output_hash"])
        self.assertEqual(a1["ledger"]["head"], a2["ledger"]["head"])
        self.assertEqual(a1["ledger"]["head"], a3["ledger"]["head"])
        for a in (a1, a2, a3):
            self.assertTrue(a["ledger"]["chain_ok"] and a["ledger"]["agrees_with_tables"])
        self.assertEqual(a1["distinct_text"], a1["drafts"])

    def test_resume_does_not_duplicate_results(self):
        snap = self.make_snapshot()
        m = self.manifest(snap, trials=10)
        B.generate(m, out_root=self.tmp / "r", chunk=10).close()
        n1 = B.audit(self.tmp / "r" / "t")["ledger"]["events"]
        B.generate(m, out_root=self.tmp / "r", chunk=10).close()
        self.assertEqual(B.audit(self.tmp / "r" / "t")["ledger"]["events"], n1)

    def test_resume_refuses_a_changed_engine(self):
        snap = self.make_snapshot()
        m = self.manifest(snap, trials=20)
        with self.assertRaises(B.Interrupted):
            B.generate(m, out_root=self.tmp / "r", chunk=10, stop_after_chunks=1)
        for field, value in (("code_hash", "x"), ("engine_version", "9.9.9"), ("schema_version", 99)):
            s = B.open_run(self.tmp / "r" / "t")
            rec = s.meta("engine")
            s.set_meta("engine", dict(rec, **{field: value}))
            s.con.commit()
            s.close()
            with self.assertRaises(ValueError) as cm:
                B.generate(m, out_root=self.tmp / "r", chunk=10)
            self.assertIn(field, str(cm.exception))
            s = B.open_run(self.tmp / "r" / "t")
            s.set_meta("engine", rec)
            s.con.commit()
            s.close()
        B.generate(m, out_root=self.tmp / "r", chunk=10).close()
        self.assertTrue(B.audit(self.tmp / "r" / "t")["complete"])

    def test_ledger_is_append_only(self):
        snap = self.make_snapshot()
        s = B.generate(self.manifest(snap, trials=3), out_root=self.tmp / "r")
        import sqlite3
        with self.assertRaises(sqlite3.DatabaseError):
            s.con.execute("UPDATE ledger SET kind='x' WHERE seq=1")
        with self.assertRaises(sqlite3.DatabaseError):
            s.con.execute("DELETE FROM ledger")
        s.close()

    def test_regeneration_matches_stored_results(self):
        snap = self.make_snapshot()
        B.generate(self.manifest(snap, trials=12), out_root=self.tmp / "r").close()
        rep = B.audit(self.tmp / "r" / "t", regenerate=0)
        self.assertEqual(rep["regeneration"]["mismatches"], [])

    def test_changing_a_preference_changes_exactly_the_predicted_drafts(self):
        base = self.make_snapshot(name="base")
        d = self.memdir()
        with open(d / "preferences.toml", "a") as f:
            f.write('\n[[pref]]\nid = "o-outreach-title"\nscope = "occasion:outreach"\nkey = "title_max_mentions"\nvalue = 1\npriority = 10\n')
        mod = self.make_snapshot(d, name="mod")
        ma = self.manifest(base, distinct=False, trials=60, name="a.toml")
        mb = self.manifest(mod, distinct=False, trials=60, name="b.toml")
        B.generate(ma, out_root=self.tmp / "ra").close()
        B.generate(mb, out_root=self.tmp / "rb").close()

        def load(run):
            s = B.open_run(self.tmp / run / "t")
            drafts = {r[0]: (r[1], r[2]) for r in s.con.execute("SELECT slot, fingerprint, body FROM draft")}
            states = dict(s.con.execute("SELECT slot, state FROM slot_state").fetchall())
            plans = {slot: unpack(blob) for slot, blob in s.con.execute("SELECT slot, plan FROM stage_a WHERE plan IS NOT NULL")}
            s.close()
            return drafts, states, plans

        da, sa, pa = load("ra")
        db, sb, pb = load("rb")
        predicted = {sl for sl, (fp, body) in da.items() if body.count(pa[sl]["fields"]["title"]) > 1}
        self.assertTrue(predicted, "the baseline must contain drafts that repeat the title")
        changed = {sl for sl in da if sb[sl] != "drafted" or db[sl][0] != da[sl][0]}
        self.assertEqual(changed, predicted)
        for sl in changed:
            if sb[sl] == "drafted":
                self.assertLessEqual(db[sl][1].count(pb[sl]["fields"]["title"]), 1)

    def test_export_accounting_and_eml_refusal(self):
        snap = self.make_snapshot()
        B.generate(self.manifest(snap, trials=5), out_root=self.tmp / "r").close()
        with self.assertRaises(ValueError):
            B.export(self.tmp / "r" / "t", "eml")
        n, out = B.export(self.tmp / "r" / "t", "jsonl", self.tmp / "out")
        lines = (out / "drafts.jsonl").read_text().strip().splitlines()
        self.assertEqual(len(lines), n)
        rec = json.loads(lines[0])
        self.assertIn("certificates", rec)
        rep = B.audit(self.tmp / "r" / "t")
        self.assertEqual(rep["states"].get("exported"), n)
        self.assertTrue(rep["all_slots_accounted"] and rep["ledger"]["agrees_with_tables"])
        B.export(self.tmp / "r" / "t", "jsonl", self.tmp / "out")
        self.assertEqual(B.audit(self.tmp / "r" / "t")["states"].get("exported"), n)

    def test_correspondence_mode_eml(self):
        snap = self.make_snapshot()
        m = self.tmp / "c.toml"
        m.write_text(f'[batch]\nid = "c"\nmode = "correspondence"\nsnapshot = "{snap}"\nseed = 7\nreference_date = "2026-11-11"\n\n[slots]\nsource = "memory"\n')
        B.generate(m, out_root=self.tmp / "r").close()
        n, out = B.export(self.tmp / "r" / "c", "eml", self.tmp / "o")
        files = sorted((out / "eml").glob("*.eml"))
        self.assertEqual(len(files), n)
        self.assertIn("X-Unsent: 1", files[0].read_text())
        self.assertFalse(list((out / "eml").glob("*.tmp")))

    def test_snapshot_detects_edits(self):
        snap = self.make_snapshot()
        (snap / "preferences.toml").write_text((snap / "preferences.toml").read_text() + "\n# edit\n")
        with self.assertRaises(ValueError):
            B.load_snapshot(snap)


if __name__ == "__main__":
    unittest.main()
