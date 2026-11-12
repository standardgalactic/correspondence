"""Offline maintenance packet for a local model such as Granite served by Ollama.

The packet holds anonymized failure traces, grammar coverage, repetitive structures,
test results and the relevant sources. A model, when asked, returns a proposed patch
that is written to a file and never applied. Accepted changes go through the normal
versioned inputs and the fixed test corpus.
"""
import json
import subprocess
import sys
import urllib.request
from pathlib import Path

from .batch import open_run, run_engine
from .util import sha256_hex


def _anon(token):
    return "anon-" + sha256_hex("maintenance:" + token)[:8]


def build_packet(run_dir, out_dir, root, run_tests=False):
    store = open_run(run_dir)
    con = store.con
    eng, mem, manifest = run_engine(store)
    batch = manifest["batch"]["id"]
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    fails = []
    for slot, state, reason in con.execute("SELECT slot, state, reason FROM slot_state WHERE batch=? AND state IN ('abstained','failed') ORDER BY idx", (batch,)):
        row = con.execute("SELECT purpose, plan_sig FROM stage_a WHERE batch=? AND slot=?", (batch, slot)).fetchone()
        fails.append({"slot": _anon(slot), "state": state, "reason": reason, "purpose": row[0] if row else None})
    summary = {}
    for f in fails:
        key = (f["state"], f["reason"])
        summary[key] = summary.get(key, 0) + 1
    usage, bigrams, constr = {}, {}, {}
    n = 0
    for (path,) in con.execute("SELECT rule_path FROM draft WHERE batch=?", (batch,)):
        n += 1
        parts = [p for p in path.split("|")[1:] if p != "-"]
        for rid in parts:
            usage[rid] = usage.get(rid, 0) + 1
        for a, b in zip(parts, parts[1:]):
            bigrams[(a, b)] = bigrams.get((a, b), 0) + 1
    rules = eng.realizer.rules
    coverage = {"drafts": n, "unused_rules": sorted(set(rules) - set(usage)),
                "most_used": sorted(usage.items(), key=lambda kv: -kv[1])[:15]}
    repetitive = [{"pair": list(k), "count": v, "share": round(v / max(1, n), 3)}
                  for k, v in sorted(bigrams.items(), key=lambda kv: -kv[1])[:15]]
    title_repeats = 0
    for subject, body, slot in con.execute("SELECT subject, body, slot FROM draft WHERE batch=?", (batch,)):
        pass
    (out / "failures.json").write_text(json.dumps({"summary": [{"state": k[0], "reason": k[1], "count": v} for k, v in summary.items()],
                                                   "examples": fails[:50]}, indent=2))
    (out / "coverage.json").write_text(json.dumps(coverage, indent=2))
    (out / "repetition.json").write_text(json.dumps(repetitive, indent=2))
    tests = "tests were not run (pass --run-tests)"
    if run_tests:
        r = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", str(Path(root) / "tests"), "-q"],
                           capture_output=True, text=True, env={"PYTHONPATH": str(Path(root) / "engine"), "PATH": "/usr/bin:/bin"})
        tests = (r.stdout + r.stderr)[-4000:]
    (out / "tests.txt").write_text(tests)
    src = out / "sources"
    src.mkdir(exist_ok=True)
    for p in sorted(Path(root, "memory", "grammar").glob("*.toml")):
        (src / p.name).write_text(p.read_text())
    for name in ("rules.py", "realize.py", "planner.py"):
        (src / name).write_text(Path(root, "engine", "corr", name).read_text())
    prompt = (
        "You are maintaining a deterministic symbolic email generator. Using only the observations in this packet, "
        "propose a patch to the grammar or evaluator that addresses one observed defect. Tie the patch to specific "
        "examples, keep the factual certificates unchanged, and include a regression fixture that checks the "
        "intended behavior. Do not invent new facts about recipients or projects.\n"
    )
    (out / "PROMPT.txt").write_text(prompt)
    store.close()
    return out


def ask_model(packet, model, url):
    packet = Path(packet)
    parts = [(packet / "PROMPT.txt").read_text()]
    for name in ("failures.json", "coverage.json", "repetition.json", "tests.txt"):
        parts.append(f"## {name}\n" + (packet / name).read_text())
    for p in sorted((packet / "sources").glob("*")):
        parts.append(f"## source {p.name}\n" + p.read_text())
    body = json.dumps({"model": model, "prompt": "\n\n".join(parts), "stream": False}).encode()
    req = urllib.request.Request(url.rstrip("/") + "/api/generate", data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=600) as r:
        text = json.loads(r.read())["response"]
    (packet / "proposed_patch.md").write_text(text)
    return packet / "proposed_patch.md"
