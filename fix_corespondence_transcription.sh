#!/usr/bin/env bash
set -euo pipefail

if (( $# != 0 && $# != 2 )); then
    printf 'Usage: %s [transcript.txt captions.vtt]\n' "${0##*/}" >&2
    exit 2
fi

stem='100,000_Zero_Hallucination_Emails_Without_LLMs'
if (( $# == 0 )); then
    set -- "$stem.txt" "$stem.vtt"
fi

python3 - "$@" <<'PY'
import re
import sys
from pathlib import Path

corrections = {
    "FlyArcs": "Flyxion",
    "Flyxion system": "Flyxion's system",
    "strong-cleaning connectives": "strong Kleene connectives",
    "strong, clean connectives": "strong Kleene connectives",
    "The clean connectives": "The Kleene connectives",
    "why do we heed this": "why do we need this",
    "the relevance row": "the relevance rho",
    "The base relevance row": "The base relevance rho",
    "why it shows silence": "why it chose silence",
    "hackers in-recepting": "hackers intercepting",
    "implication LLM": "implication lexicon",
    "bouncer's demanding": "bouncers demanding",
    "all sample drafts fail": "all sampled drafts fail",
    "the author said a hard global preference": "the author set a hard global preference",
    "And here was the kicker.": "And here's the kicker.",
    "variation exhausted.": "variation_exhausted.",
    "relevance score, which it calls Rho": "relevance score, which it calls rho",
    "a project scope preference": "a project-scoped preference",
}
corrections["proof abundance"] = "Proof Abundance"
corrections["aspect relegation"] = "Aspect Relegation"

pattern = re.compile(
    r"(?<!\w)(?:" + "|".join(map(re.escape, sorted(corrections, key=len, reverse=True)))
    + r")(?!\w)"
)

inputs = [Path(arg) for arg in sys.argv[1:]]
outputs = []
for path, suffix in zip(inputs, (".txt", ".vtt")):
    if path.suffix.lower() != suffix:
        sys.exit(f"Expected a {suffix} input: {path}")
    with path.open(encoding="utf-8", newline="") as source:
        lines = source.readlines()
    is_vtt = suffix == ".vtt"
    if is_vtt and (not lines or not lines[0].lstrip("\ufeff").startswith("WEBVTT")):
        sys.exit(f"Invalid WebVTT header: {path}")

    corrected = []
    in_cue = False
    count = 0
    for line in lines:
        if is_vtt:
            if not line.strip():
                in_cue = False
            elif re.match(r"^(?:\d+:)?\d{2}:\d{2}\.\d{3}[ \t]+-->", line):
                in_cue = True
                corrected.append(line)
                continue
        if not is_vtt or in_cue:
            line, changes = pattern.subn(lambda match: corrections[match[0]], line)
            count += changes
        corrected.append(line)

    output = path.with_name(f"{path.stem}.corrected{path.suffix}")
    outputs.append((output, corrected, count))

for output, lines, count in outputs:
    with output.open("w", encoding="utf-8", newline="") as destination:
        destination.writelines(lines)
    print(f"{output}: {count} corrections")
PY
