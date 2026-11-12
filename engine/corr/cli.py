import argparse
import json
import sys
from pathlib import Path

from . import __version__
from .batch import (Interrupted, audit, describe, export, find_slot, format_report, freeze, generate, load_manifest,
                    load_snapshot, explain)
from .engine import Engine
from .memory import load_memory, validate
from .util import parse_date


def main(argv=None):
    ap = argparse.ArgumentParser(prog="correspondence", description="Symbolic correspondence generator " + __version__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("validate"); p.add_argument("--memory", required=True)
    p = sub.add_parser("freeze"); p.add_argument("--memory", required=True); p.add_argument("--out", required=True)
    p.add_argument("--reference-date")
    p = sub.add_parser("plan"); p.add_argument("--snapshot", required=True); p.add_argument("--slot", required=True)
    p.add_argument("--manifest"); p.add_argument("--seed", type=int, default=0); p.add_argument("--occasion", default="outreach")
    p = sub.add_parser("generate"); p.add_argument("--manifest", required=True); p.add_argument("--workers", type=int, default=1)
    p.add_argument("--out", default="runs"); p.add_argument("--restart", action="store_true")
    p.add_argument("--stop-after-chunks", type=int); p.add_argument("--stop-after-slots", type=int)
    p = sub.add_parser("audit"); p.add_argument("--batch", required=True); p.add_argument("--regenerate", type=int, nargs="?", const=0)
    p.add_argument("--sample", type=int, default=0); p.add_argument("--json", action="store_true")
    p = sub.add_parser("explain"); p.add_argument("--batch", required=True); p.add_argument("--slot", required=True)
    p = sub.add_parser("export"); p.add_argument("--batch", required=True); p.add_argument("--format", required=True, choices=["jsonl", "txt", "eml"])
    p.add_argument("--out"); p.add_argument("--addresses"); p.add_argument("--omit-evidence", action="store_true")
    p = sub.add_parser("maintain"); p.add_argument("--batch", required=True); p.add_argument("--out", required=True)
    p.add_argument("--root", default="."); p.add_argument("--run-tests", action="store_true")
    p.add_argument("--model"); p.add_argument("--ollama-url", default="http://localhost:11434")
    a = ap.parse_args(argv)
    try:
        return _dispatch(a)
    except Interrupted as exc:
        print(f"interrupted: {exc}")
        return 3
    except (ValueError, KeyError, FileNotFoundError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


def _dispatch(a):
    if a.cmd == "validate":
        errs = validate(load_memory(a.memory))
        for e in errs:
            print("error:", e)
        print("valid" if not errs else f"{len(errs)} error(s)")
        return 0 if not errs else 1
    if a.cmd == "freeze":
        info = freeze(a.memory, a.out, a.reference_date)
        print(f"snapshot {a.out} hash {info['hash'][:16]} reference date {info['reference_date']} ({len(info['files'])} files)")
        return 0
    if a.cmd == "plan":
        mem, info = load_snapshot(a.snapshot)
        seed, ref, k, band = a.seed, parse_date(info["reference_date"]), 8, 0
        occasion = a.occasion
        if a.manifest:
            m = load_manifest(a.manifest)
            b = m["batch"]
            seed, ref, k, band = b["seed"], parse_date(b["reference_date"]), b["candidates_per_slot"], b["score_band"]
            occasion = m.get("slots", {}).get("occasion", occasion)
        eng = Engine(mem, seed, ref, k, band)
        print(describe(eng, find_slot(mem, a.slot, occasion)))
        return 0
    if a.cmd == "generate":
        store = generate(a.manifest, a.workers, a.out, a.restart, a.stop_after_chunks, a.stop_after_slots)
        counts = dict(store.con.execute("SELECT state, COUNT(*) FROM slot_state GROUP BY state").fetchall())
        print("generated: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
        store.close()
        return 0
    if a.cmd == "audit":
        r = audit(a.batch, a.regenerate, a.sample)
        print(json.dumps(r, indent=2) if a.json else format_report(r))
        bad = (not r["all_slots_accounted"] or not r["ledger"]["chain_ok"] or not r["ledger"]["agrees_with_tables"]
               or r.get("regeneration", {}).get("mismatches"))
        return 1 if bad else 0
    if a.cmd == "explain":
        print(explain(a.batch, a.slot))
        return 0
    if a.cmd == "export":
        n, out = export(a.batch, a.format, a.out, a.addresses, a.omit_evidence)
        print(f"exported {n} drafts as {a.format} to {out}")
        return 0
    if a.cmd == "maintain":
        from .maintain import ask_model, build_packet
        out = build_packet(a.batch, a.out, a.root, a.run_tests)
        print(f"maintenance packet written to {out}")
        if a.model:
            print("proposal written to", ask_model(out, a.model, a.ollama_url), "(not applied)")
        return 0
    return 2
