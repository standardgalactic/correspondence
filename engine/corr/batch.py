"""Snapshots, manifests, two stage generation, audit, explanation and export."""
import datetime
import json
import multiprocessing
import os
import shutil
import sqlite3
import time
import tomllib
from pathlib import Path

from . import SCHEMA_VERSION, __version__
from .engine import Engine
from .memory import load_memory, validate
from .store import Store, pack, unpack
from .util import canon, parse_date, sha256_hex

CHUNK = 500


class Interrupted(Exception):
    pass


def engine_identity():
    """Versions plus a hash of the engine source, so a code change is visible even without a version bump."""
    src = {f.name: sha256_hex(f.read_bytes()) for f in sorted(Path(__file__).parent.glob("*.py"))}
    return {"engine_version": __version__, "schema_version": SCHEMA_VERSION, "code_hash": sha256_hex(canon(src))}


# snapshots

def _files(root):
    root = Path(root)
    out = {}
    for p in sorted(root.rglob("*")):
        if p.is_file() and p.name not in ("SNAPSHOT.json", "index.sqlite"):
            out[str(p.relative_to(root))] = sha256_hex(p.read_bytes())
    return out


def snapshot_hash(files):
    return sha256_hex(canon(files))


def freeze(memory_dir, out_dir, reference_date=None):
    mem = load_memory(memory_dir)
    errs = validate(mem)
    if errs:
        raise ValueError("memory is invalid:\n" + "\n".join(errs))
    out = Path(out_dir)
    if out.exists():
        shutil.rmtree(out)
    shutil.copytree(memory_dir, out)
    files = _files(out)
    info = {
        "schema_version": SCHEMA_VERSION, "engine_version": __version__,
        "reference_date": reference_date or datetime.date.today().isoformat(),
        "files": files, "hash": snapshot_hash(files),
    }
    (out / "SNAPSHOT.json").write_text(json.dumps(info, indent=2, sort_keys=True) + "\n")
    build_index(out, mem)
    return info


def build_index(out, mem):
    """The SQLite projection used for audit queries. It is rebuilt from the declared inputs."""
    path = Path(out) / "index.sqlite"
    if path.exists():
        path.unlink()
    con = sqlite3.connect(path)
    con.executescript("""
      CREATE TABLE project(id TEXT PRIMARY KEY, title TEXT, status TEXT, kind TEXT);
      CREATE TABLE recipient(id TEXT PRIMARY KEY, language TEXT);
      CREATE TABLE evidence(id TEXT PRIMARY KEY, subject TEXT, predicate TEXT, project TEXT, observed TEXT, uses TEXT);
      CREATE INDEX evidence_subject ON evidence(subject, predicate);
    """)
    con.executemany("INSERT INTO project VALUES(?,?,?,?)",
                    [(p["id"], p["title"], p["status"], p["kind"]) for p in mem.projects.values()])
    con.executemany("INSERT INTO recipient VALUES(?,?)", [(r["id"], r["language"]) for r in mem.recipients.values()])
    con.executemany("INSERT INTO evidence VALUES(?,?,?,?,?,?)",
                    [(e["id"], e["subject"], e["predicate"], e.get("project"), e["observed"], ",".join(e["uses"]))
                     for lst in mem.evidence.values() for e in lst])
    con.commit()
    con.close()


def load_snapshot(path):
    path = Path(path)
    info = json.loads((path / "SNAPSHOT.json").read_text())
    files = _files(path)
    if snapshot_hash(files) != info["hash"]:
        raise ValueError(f"snapshot {path} no longer matches its recorded hash")
    return load_memory(path), info


# manifests and slots

def load_manifest(path):
    with open(path, "rb") as f:
        data = tomllib.load(f)
    b = data["batch"]
    b.setdefault("candidates_per_slot", 8)
    b.setdefault("score_band", 0)
    b.setdefault("require_distinct", b["mode"] == "experiment")
    data["manifest_hash"] = sha256_hex(canon(data))
    data["path"] = str(path)
    return data


def resolve_snapshot(manifest):
    p = Path(manifest["batch"]["snapshot"])
    if p.exists():
        return p
    alt = Path(manifest["path"]).resolve().parent.parent / p
    if alt.exists():
        return alt
    raise FileNotFoundError(f"snapshot {p} not found")


def slot_count(manifest, mem):
    s = manifest.get("slots", {})
    if manifest["batch"]["mode"] == "experiment":
        return len(s["scenarios"]) * int(s["trials"])
    return len(_memory_slots(manifest, mem))


def _memory_slots(manifest, mem):
    ids = manifest.get("slots", {}).get("ids")
    return [dict(s) for s in mem.slots if s["mode"] == "correspondence" and (not ids or s["id"] in ids)]


def slot_at(manifest, mem, idx, cache=None):
    s = manifest.get("slots", {})
    if manifest["batch"]["mode"] == "experiment":
        sc = s["scenarios"]
        scenario, trial = sc[idx % len(sc)], idx // len(sc) + 1
        slot = {"id": f"exp-{scenario}-{trial:07d}", "mode": "experiment", "recipient": scenario,
                "occasion": s.get("occasion", "outreach"), "trial": trial}
        if s.get("purpose"):
            slot["purpose"] = s["purpose"]
        return slot
    lst = cache if cache is not None else _memory_slots(manifest, mem)
    return lst[idx]


def find_slot(mem, slot_id, occasion="outreach"):
    for s in mem.slots:
        if s["id"] == slot_id:
            return dict(s)
    import re
    m = re.match(r"^exp-(.+)-(\d+)$", slot_id)
    if m and m.group(1) in mem.recipients:
        return {"id": slot_id, "mode": "experiment", "recipient": m.group(1), "occasion": occasion,
                "trial": int(m.group(2))}
    raise KeyError(f"unknown slot {slot_id!r}")


# generation

_W = {}


def _init_worker(snapshot, seed, ref, k, band):
    _W["engine"] = Engine(load_memory(snapshot), seed, ref, k, band)


def _run(engine, idx, slot):
    out = engine.run_slot(slot)
    plan = out.get("plan")
    cands = "\n".join(f"{c['id']}\t{c['fp']}\t{c['sig']}" for c in out.get("candidates", []))
    return (idx, slot["id"], out["status"], out.get("reason"), pack(plan) if plan else None, cands,
            plan["purpose"] if plan else None, plan["plan_sig"] if plan else None, out.get("fallback"))


def _work(task):
    return [_run(_W["engine"], idx, slot) for idx, slot in task]


def generate(manifest_path, workers=1, out_root="runs", restart=False, stop_after_chunks=None,
             stop_after_slots=None, chunk=CHUNK, log=print):
    holder = []
    try:
        return _generate(manifest_path, workers, out_root, restart, stop_after_chunks, stop_after_slots, chunk, log, holder)
    except BaseException:
        for st in holder:
            st.close()
        raise


def _generate(manifest_path, workers, out_root, restart, stop_after_chunks, stop_after_slots, chunk, log, holder):
    manifest = load_manifest(manifest_path)
    b = manifest["batch"]
    snap = resolve_snapshot(manifest)
    mem, info = load_snapshot(snap)
    if b.get("expect_snapshot_hash") and b["expect_snapshot_hash"] != info["hash"]:
        raise ValueError("snapshot hash does not match the manifest")
    ref = parse_date(b["reference_date"])
    run_dir = Path(out_root) / b["id"]
    if restart and run_dir.exists():
        shutil.rmtree(run_dir)
    store = Store(run_dir)
    holder.append(store)
    con = store.con
    batch = b["id"]
    total = slot_count(manifest, mem)
    cache = _memory_slots(manifest, mem) if b["mode"] == "correspondence" else None
    if store.meta("manifest_hash") is None:
        rows = []
        for idx in range(total):
            rows.append((batch, slot_at(manifest, mem, idx, cache)["id"], idx, "pending", None))
            if len(rows) >= 100000:
                con.executemany("INSERT INTO slot_state VALUES(?,?,?,?,?)", rows)
                rows = []
        con.executemany("INSERT INTO slot_state VALUES(?,?,?,?,?)", rows)
        store.set_meta("manifest_hash", manifest["manifest_hash"])
        store.set_meta("manifest", {k: v for k, v in manifest.items() if k not in ("path",)})
        store.set_meta("snapshot", {"path": str(Path(snap).resolve()), "hash": info["hash"]})
        store.set_meta("slots_total", total)
        store.set_meta("engine", engine_identity())
        store.ledger("batch_created", None, manifest["manifest_hash"], {
            "snapshot_hash": info["hash"], "engine_version": __version__, "schema_version": SCHEMA_VERSION,
            "seed": b["seed"], "reference_date": b["reference_date"], "slots": total, "mode": b["mode"]})
        con.commit()
    elif store.meta("manifest_hash") != manifest["manifest_hash"]:
        raise ValueError("run directory holds a different manifest; use --restart to discard it")
    elif store.meta("snapshot")["hash"] != info["hash"]:
        raise ValueError("run directory was started from a different snapshot")
    else:
        recorded, current = store.meta("engine"), engine_identity()
        if recorded is None:
            raise ValueError("run directory has no recorded engine identity; use --restart")
        diff = [k for k in current if recorded.get(k) != current[k]]
        if diff:
            raise ValueError("engine changed since this run started (" + ", ".join(diff)
                             + "); resuming would mix results from two engines. Use --restart to regenerate")

    args = (str(snap), b["seed"], ref, b["candidates_per_slot"], b["score_band"])
    timing = store.meta("timing", {"stage_a": 0.0, "stage_b": 0.0})
    # stage A: bounded plans and candidate sets, in disjoint chunks
    t0 = time.time()
    pending = [r[0] for r in con.execute("SELECT idx FROM slot_state WHERE batch=? AND state='pending' ORDER BY idx", (batch,))]
    chunks = [pending[i:i + chunk] for i in range(0, len(pending), chunk)]

    def tasks():
        for ch in chunks:
            yield [(i, slot_at(manifest, mem, i, cache)) for i in ch]

    def commit_chunk(results):
        for (idx, sid, status, reason, plan, cands, purpose, psig, fb) in results:
            con.execute("INSERT OR REPLACE INTO stage_a VALUES(?,?,?,?,?,?,?,?,?)",
                        (batch, sid, status, reason, plan, cands, purpose, psig, fb))
            con.execute("UPDATE slot_state SET state='staged' WHERE batch=? AND slot=?", (batch, sid))
        con.commit()

    done = 0
    try:
        if workers <= 1:
            eng = Engine(mem, b["seed"], ref, b["candidates_per_slot"], b["score_band"])
            for task in tasks():
                commit_chunk([_run(eng, i, s) for i, s in task])
                done += 1
                if stop_after_chunks and done >= stop_after_chunks:
                    raise Interrupted("stopped after stage A chunks")
        else:
            ctx = multiprocessing.get_context("fork")
            with ctx.Pool(workers, initializer=_init_worker, initargs=args) as pool:
                for results in pool.imap_unordered(_work, tasks()):
                    commit_chunk(results)
                    done += 1
                    if stop_after_chunks and done >= stop_after_chunks:
                        raise Interrupted("stopped after stage A chunks")
    finally:
        timing["stage_a"] += time.time() - t0
        store.set_meta("timing", timing)
        con.commit()

    # stage B: assignment in stable slot order
    t1 = time.time()
    eng = Engine(mem, b["seed"], ref, b["candidates_per_slot"], b["score_band"])
    try:
        _stage_b(store, eng, b, batch, stop_after_slots, log)
    finally:
        timing["stage_b"] += time.time() - t1
        store.set_meta("timing", timing)
        con.commit()
    left = con.execute("SELECT COUNT(*) FROM slot_state WHERE batch=? AND state IN ('pending','staged')", (batch,)).fetchone()[0]
    if left == 0 and not store.meta("completed"):
        counts = dict(con.execute("SELECT state, COUNT(*) FROM slot_state WHERE batch=? GROUP BY state", (batch,)).fetchall())
        store.ledger("batch_completed", None, manifest["manifest_hash"], {"counts": counts})
        store.set_meta("completed", True)
        con.commit()
    return store


def _stage_b(store, eng, b, batch, stop_after, log):
    con = store.con
    distinct = bool(b["require_distinct"])
    last, processed = -1, 0
    while True:
        limit = 2000
        if stop_after:
            limit = min(limit, stop_after - processed)
            if limit <= 0:
                raise Interrupted("stopped after stage B slots")
        rows = con.execute(
            "SELECT s.idx,s.slot,a.status,a.reason,a.plan,a.cands,a.plan_sig,a.fallback FROM slot_state s "
            "JOIN stage_a a ON a.batch=s.batch AND a.slot=s.slot "
            "WHERE s.batch=? AND s.state='staged' AND s.idx>? ORDER BY s.idx LIMIT ?", (batch, last, limit)).fetchall()
        if not rows:
            break
        for idx, slot, status, reason, plan_blob, cands, psig, fb in rows:
            last = idx
            processed += 1
            if status == "abstained":
                store.ledger("slot_abstained", slot, None, {"reason": reason})
                con.execute("UPDATE slot_state SET state='abstained', reason=? WHERE batch=? AND slot=?", (reason, batch, slot))
                continue
            if status == "failed":
                store.ledger("slot_failed", slot, None, {"reason": reason})
                con.execute("UPDATE slot_state SET state='failed', reason=? WHERE batch=? AND slot=?", (reason, batch, slot))
                continue
            plan = unpack(plan_blob)
            store.ledger("slot_planned", slot, plan["plan_hash"], {
                "purpose": plan["purpose"], "goal": plan["goal"], "project": plan["project"], "score": plan["score"]})
            cl = [dict(zip(("id", "fp", "sig"), line.split("\t"))) for line in cands.split("\n")]
            chosen = None
            for c in eng.select(slot, cl):
                if distinct:
                    cur = con.execute("INSERT OR IGNORE INTO fingerprint VALUES(?,?,?)", (batch, c["fp"], slot))
                    if cur.rowcount != 1:
                        continue
                chosen = c
                break
            if chosen is None:
                store.ledger("slot_failed", slot, plan["plan_hash"], {"reason": "variation_exhausted"})
                con.execute("UPDATE slot_state SET state='failed', reason='variation_exhausted' WHERE batch=? AND slot=?",
                            (batch, slot))
                continue
            built = eng.render(plan, chosen["id"])
            if not built["ok"] or built["fp"] != chosen["fp"]:
                store.ledger("slot_failed", slot, plan["plan_hash"], {"reason": "rerender_mismatch"})
                con.execute("UPDATE slot_state SET state='failed', reason='rerender_mismatch' WHERE batch=? AND slot=?",
                            (batch, slot))
                continue
            con.execute("INSERT INTO draft VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (batch, slot, built["subject"], built["body"], built["fp"], built["sig"], plan["plan_sig"],
                         built["id"], plan["purpose"], plan["project"]))
            store.ledger("slot_rendered", slot, built["fp"], {"rule_path": built["id"], "fallback": fb})
            store.ledger("slot_validated", slot, plan["plan_hash"], {
                "certificates": len([c for c in plan["certificates"] if c["use"] == "mention"])})
            con.execute("UPDATE slot_state SET state='drafted' WHERE batch=? AND slot=?", (batch, slot))
        con.commit()


# reading results

def open_run(run_dir):
    return Store(run_dir)


def run_engine(store):
    manifest = store.meta("manifest")
    snap = store.meta("snapshot")
    mem, info = load_snapshot(snap["path"])
    b = manifest["batch"]
    eng = Engine(mem, b["seed"], parse_date(b["reference_date"]), b["candidates_per_slot"], b["score_band"])
    return eng, mem, manifest


def audit(run_dir, regenerate=None, sample=0):
    store = open_run(run_dir)
    con = store.con
    manifest = store.meta("manifest")
    b = manifest["batch"]
    batch = b["id"]
    total = store.meta("slots_total")
    ok, n = store.verify_ledger()
    counts = dict(con.execute("SELECT state, COUNT(*) FROM slot_state WHERE batch=? GROUP BY state", (batch,)).fetchall())
    reasons = con.execute("SELECT state, reason, COUNT(*) FROM slot_state WHERE batch=? AND reason IS NOT NULL "
                          "GROUP BY state, reason ORDER BY state, COUNT(*) DESC", (batch,)).fetchall()
    ledger_counts = dict(con.execute("SELECT kind, COUNT(*) FROM ledger GROUP BY kind").fetchall())
    exported_slots = con.execute("SELECT COUNT(DISTINCT slot) FROM ledger WHERE kind='slot_exported'").fetchone()[0]
    drafted_total = counts.get("drafted", 0) + counts.get("exported", 0)
    ledger_agrees = (ledger_counts.get("slot_abstained", 0) == counts.get("abstained", 0)
                     and ledger_counts.get("slot_failed", 0) == counts.get("failed", 0)
                     and ledger_counts.get("slot_validated", 0) == drafted_total
                     and exported_slots == counts.get("exported", 0))
    accounted = sum(counts.get(k, 0) for k in ("drafted", "exported", "abstained", "failed"))
    d = con.execute("SELECT COUNT(*), COUNT(DISTINCT fingerprint), COUNT(DISTINCT struct_sig), COUNT(DISTINCT plan_sig) "
                    "FROM draft WHERE batch=?", (batch,)).fetchone()
    by_purpose = dict(con.execute("SELECT purpose, COUNT(*) FROM draft WHERE batch=? GROUP BY purpose", (batch,)).fetchall())
    h = sha256_hex("")
    import hashlib
    hasher = hashlib.sha256()
    for slot, fp in con.execute("SELECT d.slot, d.fingerprint FROM draft d JOIN slot_state s ON s.batch=d.batch AND s.slot=d.slot "
                                "WHERE d.batch=? ORDER BY s.idx", (batch,)):
        hasher.update(f"{slot}\t{fp}\n".encode())
    usage = {}
    for (path,) in con.execute("SELECT rule_path FROM draft WHERE batch=?", (batch,)):
        for rid in path.split("|")[1:]:
            if rid != "-":
                usage[rid] = usage.get(rid, 0) + 1
    eng, mem, _ = run_engine(store)
    unused = sorted(set(eng.realizer.rules) - set(usage))
    timing = store.meta("timing", {})
    wall = timing.get("stage_a", 0) + timing.get("stage_b", 0)
    report = {
        "batch": batch, "mode": b["mode"], "slots_requested": total,
        "complete": counts.get("pending", 0) + counts.get("staged", 0) == 0,
        "states": counts, "accounted": accounted, "all_slots_accounted": accounted == total,
        "reasons": [{"state": s, "reason": r, "count": c} for s, r, c in reasons],
        "drafts": d[0], "distinct_text": d[1], "distinct_structure": d[2], "distinct_plan": d[3],
        "shortfall": total - d[0], "by_purpose": by_purpose,
        "capacity_shortfall": counts.get("failed", 0),
        "ledger": {"events": n, "chain_ok": ok, "head": store.last, "counts": ledger_counts, "agrees_with_tables": ledger_agrees},
        "output_hash": hasher.hexdigest(), "rules_unused": unused,
        "throughput": {"seconds": round(wall, 2), "slots_per_second": round(total / wall, 1) if wall else None,
                       "stage_a_seconds": round(timing.get("stage_a", 0), 2), "stage_b_seconds": round(timing.get("stage_b", 0), 2)},
    }
    if regenerate is not None:
        report["regeneration"] = _regenerate(store, eng, mem, manifest, regenerate)
    if sample:
        report["sample"] = _sample(store, batch, sample, b["seed"])
    store.close()
    return report


def _regenerate(store, eng, mem, manifest, n):
    con = store.con
    batch = manifest["batch"]["id"]
    rows = con.execute("SELECT s.slot, s.state, s.reason, d.rule_path, d.fingerprint FROM slot_state s "
                       "LEFT JOIN draft d ON d.batch=s.batch AND d.slot=s.slot WHERE s.batch=? ORDER BY s.idx", (batch,)).fetchall()
    if n:
        step = max(1, len(rows) // n)
        rows = rows[::step][:n]
    bad = []
    for slot_id, state, reason, path, fp in rows:
        slot = find_slot(mem, slot_id, manifest.get("slots", {}).get("occasion", "outreach"))
        out = eng.run_slot(slot)
        if state == "abstained":
            if out["status"] != "abstained" or out["reason"] != reason:
                bad.append(slot_id)
        elif state in ("drafted", "exported"):
            if out["status"] != "planned" or not any(c["id"] == path and c["fp"] == fp for c in out["candidates"]):
                bad.append(slot_id)
    return {"checked": len(rows), "mismatches": bad}


def _sample(store, batch, n, seed):
    from .util import h64
    rows = store.con.execute("SELECT slot, purpose, rule_path FROM draft WHERE batch=?", (batch,)).fetchall()
    by = {}
    for slot, purpose, path in rows:
        by.setdefault(purpose, []).append((h64(seed, "sample", slot), slot))
    picked = []
    per = max(1, n // max(1, len(by)))
    for purpose in sorted(by):
        picked += [s for _, s in sorted(by[purpose])[:per]]
    out = []
    for slot in picked:
        subj, body, path = store.con.execute("SELECT subject, body, rule_path FROM draft WHERE batch=? AND slot=?", (batch, slot)).fetchone()
        out.append({"slot": slot, "subject": subj, "body": body, "rule_path": path})
    return out


def format_report(r):
    L = []
    L.append(f"batch {r['batch']} ({r['mode']}): {r['slots_requested']} slots requested, "
             f"{'complete' if r['complete'] else 'INCOMPLETE'}")
    L.append("states: " + ", ".join(f"{k}={v}" for k, v in sorted(r["states"].items())))
    L.append(f"accounting: {r['accounted']} of {r['slots_requested']} slots reached drafted, exported, abstained or failed"
             + ("" if r["all_slots_accounted"] else "  <-- NOT ALL ACCOUNTED"))
    for x in r["reasons"]:
        L.append(f"  {x['state']}: {x['reason']} x{x['count']}")
    L.append(f"drafts: {r['drafts']}  distinct text: {r['distinct_text']}  distinct structure: {r['distinct_structure']}"
             f"  distinct plan: {r['distinct_plan']}  shortfall vs requested: {r['shortfall']}"
             f" (of which {r['capacity_shortfall']} failed to render, the rest abstained)")
    L.append("drafts by purpose: " + ", ".join(f"{k}={v}" for k, v in sorted(r["by_purpose"].items())))
    t = r["throughput"]
    L.append(f"throughput: {t['slots_per_second']} slots/s over {t['seconds']}s "
             f"(stage A {t['stage_a_seconds']}s, stage B {t['stage_b_seconds']}s)")
    lg = r["ledger"]
    L.append(f"ledger: {lg['events']} events, chain {'ok' if lg['chain_ok'] else 'BROKEN'}, "
             f"{'agrees' if lg['agrees_with_tables'] else 'DISAGREES'} with tables, head {lg['head'][:16]}")
    L.append(f"output hash: {r['output_hash'][:16]}")
    L.append(f"grammar rules never used: {len(r['rules_unused'])}" + (f" ({', '.join(r['rules_unused'][:12])})" if r["rules_unused"] else ""))
    if "regeneration" in r:
        g = r["regeneration"]
        L.append(f"regeneration: {g['checked']} slots checked, {len(g['mismatches'])} mismatches")
    for s in r.get("sample", []):
        L.append(f"\n--- {s['slot']}  [{s['rule_path']}]\nSubject: {s['subject']}\n\n{s['body']}")
    return "\n".join(L)


def explain(run_dir, slot_id):
    store = open_run(run_dir)
    eng, mem, manifest = run_engine(store)
    batch = manifest["batch"]["id"]
    slot = find_slot(mem, slot_id, manifest.get("slots", {}).get("occasion", "outreach"))
    row = store.con.execute("SELECT s.state, s.reason, d.rule_path, d.subject, d.body FROM slot_state s "
                            "LEFT JOIN draft d ON d.batch=s.batch AND d.slot=s.slot WHERE s.batch=? AND s.slot=?",
                            (batch, slot_id)).fetchone()
    text = describe(eng, slot, row)
    store.close()
    return text


def describe(eng, slot, row=None, with_candidates=True):
    slot_id = slot["id"]
    out = eng.run_slot(slot, debug=True)
    L = [f"slot {slot_id}: recipient {slot['recipient']}, occasion {slot['occasion']}"]
    L.append("goal evaluation:")
    for c in out.get("evaluation", []):
        L.append(f"  {c['goal']} on {c['project']}: active={c['active']} requires={c.get('requires')} "
                 f"disqualified={c.get('disqualified')} eligible={c['eligible']} score={c.get('score')}"
                 + (f" ({c['why']})" if c.get("why") else ""))
        for t in c.get("requires_trace", []):
            L.append(f"      requires {t['op']}: {t['result']} ({t['detail']})")
        for t in c.get("disqualify_trace", []):
            L.append(f"      disqualify {t['op']}: {t['result']} ({t['detail']})")
        if c.get("score_parts"):
            L.append(f"      score parts {c['score_parts']}")
    L.append(f"plan status: {out['status']}" + (f" ({out['reason']})" if out.get("reason") else ""))
    if out.get("plan"):
        p = out["plan"]
        L.append(f"chosen goal {p['goal']} on {p['project']}, request {p['request']['action']}, "
                 f"foreground {p['aspects']['foreground']}, excluded {p['aspects']['excluded']}")
        L.append("certificates:")
        for c in p["certificates"]:
            L.append(f"  {c['id']} {c['kind']} [{c['use']}] via {c['rule']} from {c['sources']}: {c['proposition']}")
        L.append(f"constraints in force: {json.dumps(p['constraints'], sort_keys=True)}")
        ranked = eng.select(slot_id, out.get("candidates", []))
        L.append(f"candidates ({len(ranked)} valid, {len(out.get('rejected', []))} rejected):")
        for i, c in enumerate(ranked):
            L.append(f"  #{i} {c['id']}  fp={c['fp'][:10]} words={c['words']}")
        for r in out.get("rejected", []):
            L.append(f"  rejected {r['id']}: {r['error']}")
        if row and row[2]:
            L.append(f"stored selection: {row[2]}")
            same = any(c["id"] == row[2] for c in ranked)
            L.append("deterministic regeneration: " + ("stored candidate was reproduced" if same else "STORED CANDIDATE NOT REPRODUCED"))
            L.append("rule path:")
            for rid in row[2].split("|")[1:]:
                if rid in eng.realizer.rules:
                    L.append(f"  {rid}: {eng.realizer.rules[rid]['text']!r}")
            L.append(f"\nSubject: {row[3]}\n\n{row[4]}")
    if row:
        L.append(f"stored state: {row[0]}" + (f" ({row[1]})" if row[1] else ""))
    return "\n".join(L)


def export(run_dir, fmt, out=None, addresses=None, omit_evidence=False):
    store = open_run(run_dir)
    con = store.con
    manifest = store.meta("manifest")
    b = manifest["batch"]
    batch = b["id"]
    if fmt == "eml" and b["mode"] == "experiment":
        raise ValueError("experiment drafts have fictional recipients and cannot be exported as .eml")
    out = Path(out or (Path(run_dir) / "export"))
    out.mkdir(parents=True, exist_ok=True)
    eng, mem, _ = run_engine(store)
    addr = {}
    if addresses:
        with open(addresses, "rb") as f:
            addr = tomllib.load(f)
    rows = con.execute("SELECT s.idx, s.slot, s.state, d.subject, d.body, d.fingerprint, d.struct_sig, d.plan_sig, d.rule_path, "
                       "d.purpose, d.project, a.plan FROM slot_state s JOIN draft d ON d.batch=s.batch AND d.slot=s.slot "
                       "JOIN stage_a a ON a.batch=s.batch AND a.slot=s.slot WHERE s.batch=? AND s.state IN ('drafted','exported') "
                       "ORDER BY s.idx", (batch,)).fetchall()
    events = []

    def atomic(path, data):
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_bytes(data if isinstance(data, bytes) else data.encode("utf-8"))
        os.replace(tmp, path)

    if fmt == "jsonl":
        lines = []
        for idx, slot, state, subj, body, fp, ssig, psig, path, purpose, project, plan in rows:
            plan = unpack(plan)
            certs = [{k: v for k, v in c.items() if k != "fields"} for c in plan["certificates"] if c["use"] == "mention"]
            if omit_evidence:
                for c in certs:
                    c["proposition"] = "(omitted)"
            rec = {"batch": batch, "slot": slot, "recipient": plan["recipient"], "purpose": purpose, "project": project,
                   "subject": subj, "body": body, "fingerprint": fp, "structural_signature": ssig, "plan_signature": psig,
                   "rule_path": path, "certificates": certs}
            line = json.dumps(rec, ensure_ascii=False, sort_keys=True)
            lines.append(line)
            events.append((slot, state, sha256_hex(line)))
        atomic(out / "drafts.jsonl", "\n".join(lines) + ("\n" if lines else ""))
    elif fmt == "txt":
        chunks = []
        for idx, slot, state, subj, body, fp, ssig, psig, path, purpose, project, plan in rows:
            text = f"=== {slot} ===\nSubject: {subj}\n\n{body}\n"
            chunks.append(text)
            events.append((slot, state, sha256_hex(text)))
        atomic(out / "drafts.txt", "\n".join(chunks))
    elif fmt == "eml":
        (out / "eml").mkdir(exist_ok=True)
        for idx, slot, state, subj, body, fp, ssig, psig, path, purpose, project, plan in rows:
            plan = unpack(plan)
            rec = mem.recipients[plan["recipient"]]
            to = addr.get(rec.get("address_ref", ""), "") if rec.get("address_ref") else ""
            msg = (f"To: {to or '(address unresolved)'}\nSubject: {subj}\nX-Unsent: 1\nMIME-Version: 1.0\n"
                   f"Content-Type: text/plain; charset=utf-8\n\n{body}\n")
            atomic(out / "eml" / f"{slot}.eml", msg)
            events.append((slot, state, sha256_hex(msg)))
    else:
        raise ValueError(f"unknown format {fmt!r}")
    for slot, state, h in events:
        if state == "drafted":
            store.ledger("slot_exported", slot, h, {"format": fmt})
            con.execute("UPDATE slot_state SET state='exported' WHERE batch=? AND slot=?", (batch, slot))
    con.commit()
    store.close()
    return len(rows), out
