# Symbolic Correspondence Generator

The Symbolic Correspondence Generator is a research prototype for composing email drafts from explicit project goals, recipient evidence, and author preferences. It represents each draft first as a message plan, then realizes that plan through a typed grammar. This separation makes it possible to vary the wording while preserving the purpose and the requested action, and to examine why a particular message was generated.

The engine runs locally with Python and SQLite. Bash provides the command-line entry point and composes the demonstration workflow. Planning and realization require no language model, network connection, or mail account. See the [technical specification](correspondence-spec.pdf) and its [LaTeX source](correspondence-spec.tex) for the design and acceptance criteria.

## Usage

Run the commands from the repository root with Bash and Python 3.11 or later.

```bash
bin/correspondence validate --memory memory/
bin/correspondence freeze --memory memory/ --out snapshots/proof-abundance --reference-date 2026-11-11
bin/correspondence plan --snapshot snapshots/proof-abundance --slot corr-012-lee --seed 20261111
bin/correspondence generate --manifest batches/correspondence.toml
bin/correspondence audit --batch runs/correspondence --regenerate
bin/correspondence explain --batch runs/correspondence --slot corr-008-hana
bin/correspondence export --batch runs/correspondence --format txt
```

`plan` reports the goals considered, evidence requirements, scoring decisions, and certificates before rendering. `explain` traces an individual slot through planning and realization, including rejected candidates and the grammar rules used by the selected draft. Each subcommand accepts `--help`.

`bin/run-first-release` validates the sample memory, freezes its snapshot, runs the tests, and generates and audits both sample batches. It restarts the existing `runs/correspondence` and `runs/experiment` outputs. Correspondence exports are available as plain text and `.eml` drafts. The sample recipients are fictional, and resolving a `To` address requires an address file. Experiment output can be exported as JSON Lines with evidence values omitted.

## Planning and realization

The [sample memory](memory/) contains five projects from the essay collection, twelve fictional recipient histories, and three purposes: requesting typesetting corrections, following up on substantive feedback, and sharing a requested revision. Project summaries and revision descriptions serve as example data. The histories deliberately overlap so that the planner must distinguish, for example, a download from a substantive comment, or an offer to help on one project from an offer that applies to another.

The planner evaluates goal eligibility using three-valued logic. Missing evidence evaluates to unknown. An unknown positive requirement cannot establish eligibility, and an unknown disqualifier is recorded in the trace without blocking the plan. Eligible plans receive integer scores under a versioned rule. The seed governs selection within the configured score band. When no admissible plan exists, the slot records an abstention and its reason.

Scope-carrying certificates associate planned assertions with their supporting evidence and permitted use. A record available only for planning can affect eligibility while remaining unavailable to the renderer. Aspect Relegation is represented through explicit choices about which project details the plan foregrounds, makes optional, or excludes. A typesetting request, for example, can foreground the title and defect while leaving the essay’s argument out of the draft.

The [grammar](memory/grammar/) declares purposes, discourse moves, referent dependencies, and pragmatic requirements for individual constructions. The renderer checks candidate drafts against these declarations and scans for configured phrases whose presuppositions require certificates. It also applies constraints on length, title mentions, and request structure. These checks cover the declared grammar and configured text patterns. They are bounded checks on the realized prose.

## Reproducibility and evaluation

The manifest fixes the seed and reference date, and a frozen snapshot identifies the input corpus. Workers produce bounded candidate sets independently. A single writer assigns candidates in stable slot order and reserves text fingerprints for experiment batches that require distinct drafts. SQLite records slot outcomes and a hash-chained event ledger. The audit reports ledger consistency, draft and abstention counts, and distinct text, structural, and plan signatures.

Serial, two-worker, and interrupted runs of the 10,008-slot experiment produced identical output hashes under the tested engine and schema versions. Resume currently checks the manifest and snapshot hashes. Resume a batch with the same engine and schema versions used to start it.

| Synthetic experiment | Slots | Distinct drafts | Observed outcome |
| --- | ---: | ---: | --- |
| Standard experiment | 10,008 | 5,838 | 4,170 abstentions and zero rendering failures |
| Extended experiment | 100,008 | 58,336 | About 60 seconds on two cores and two `variation_exhausted` slots |

The standard experiment produced 76 structural signatures and 27 plan signatures. The extended run’s two exhaustion outcomes indicate that the bounded search found no eligible unused candidate for those slots. The [million-slot benchmark](batches/benchmark.toml) is defined but has yet to be measured.

The test suite exercises evidence dependence, implication checks, preference changes, deterministic replay, snapshot integrity, and export accounting.

```bash
PYTHONPATH=engine python3 -m unittest discover -s tests
```

## Offline grammar maintenance

`bin/correspondence maintain --batch runs/experiment --out maintenance/packet` prepares anonymized failure traces, grammar coverage, repeated rule pairs, and relevant source files for inspection. The optional `--run-tests` flag includes test output. A local Ollama model can analyze the packet and return a proposed patch for review. Accepted changes enter the versioned grammar or evaluator through the normal test workflow.

## License

Released under the [MIT License](LICENSE).
