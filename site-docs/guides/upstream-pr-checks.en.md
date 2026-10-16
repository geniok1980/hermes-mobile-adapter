# PR checks in the Hermes core: what our catalog repin goes through

This note describes the path of a pull request in `NousResearch/hermes-agent` from opening to
merge — using our PR [#138527](https://github.com/NousResearch/hermes-agent/pull/138527) (a repin of
the `hermes-mobile` plugin-catalog entry) as the example. The data was read from the live PR on
16 October 2026.

## In short: three gates before merge

| Gate | Who | What it checks | How it looks in the PR |
|---|---|---|---|
| 1. Runs allowed | a maintainer clears the first-run gate | that the PR may execute on runners at all | until approved every run reports `action_required` |
| 2. Runs (5 workflows, 52 checks) | repository automation | that the change does not break the core and is valid for the catalog | `28 ✅ / 24 ⏭`, zero failures |
| 3. Review and merge | a maintainer | the content of the change | `reviewDecision` non-empty → the merge button becomes active |

**The important part:** green runs do not merge anything by themselves. Without a review GitHub
keeps `mergeStateStatus = BLOCKED` even when every check passed.

## Gate 1. Someone has to allow the runs

For a first-time contributor the first workflow run needs a maintainer's approval — that keeps
foreign code from executing on runners. Until then every run reports `completed / action_required`
(no work done), and `gh pr checks` prints an empty list — which is why “no checks” does not mean
“no checks required”.

In our case a maintainer cleared it: the `ci-reviewed` label appeared on 16 October at 07:13–07:16
UTC, and the runs completed on their second attempt (`run_attempt: 2`).

## Gate 2. What actually runs

Five workflows on the PR head (`fb502e35…`, our repin):

| Workflow | Result | What it proves |
|---|---|---|
| **Plugin Catalog CI** | ✅ success | the catalog entry is structurally valid and the pin points at an existing commit |
| **CI** | ✅ success | the change breaks nothing: lints, Python tests, OS-specific suites, JS/TS, Rust, docs and dozens of structural checks |
| **Docker Build, Test, and Publish** | ✅ success | the image builds and passes its tests (nothing is published from a PR) |
| **Nix flake check** | ✅ success | the Nix build is reproducible |
| **Label rerun** | `action_required` | repository automation (relabel-triggered rerun), unrelated to our change |

### Plugin Catalog CI — the part that concerns us directly

Two jobs, both green:

* `structural` — entry shape: required fields, `sha` format, versions, `tier`/`category`/
  `requires_hermes` consistency;
* `pinned-source-validate` — the pin resolves to a real commit in the declared repository and the
  declared `subdir` contains a plugin manifest (`plugin.yaml`) whose `version` matches the catalog
  entry.

Those two answer “the entry is neither stale nor broken”: our pin `91bdca08…` (plugin 1.1.1) passed
them.

### CI — the large run (what is inside)

`CI` is a composite workflow pulling in 25 nested check sets. All of them finished successfully; for
orientation, what was exercised:

* **Python lints:** `ruff enforcement`, `ruff + ty diff`, `code health ratchet`, `Windows footguns`;
* **Python tests:** `Run tests (1/2)`, `Run tests (2/2)`, `Python tests / e2e`, `e2e-upgrade`,
  `e2e-upgrade-plan`;
* **OS-specific:** macOS tests (2 parts), Windows tests (regular and arm64, 2 parts each),
  `Windows install + update E2E` driving real processes;
* **JS and Rust:** `JS & TS checks`, `Rust tests`;
* **Structural/repo-wide:** large binaries, contributor attribution, committed infographics, file-name
  case collisions, common history with the base branch, icon freshness, production imports of the
  `tools.lazy_deps` stub, profile artifacts, `prompt surface diff`, `Supply-chain scan`, remote
  terminal backends;
* **Dependencies:** `uv.lock`, `package-lock.json diff`, `lockfile-diff`;
* **Docs and build:** `Docs Site`, `Bootstrap installer`, `Lint Docker scripts`;
* **Desktop:** `Desktop E2E`, `Desktop core E2E`, `Desktop update E2E` — conditional (see below);
* **Final:** `All required checks pass (v2)` — the summary check GitHub itself uses for merging.

### Why some checks are “skipped”

24 of the 52 checks report `⏭ skipped` — that is neither a failure nor an oversight: they are
conditional steps that only run where they apply (docker image build and publish, release-manifest
publishing, desktop E2E, the full OS matrix on release branches). For a three-line change in a
catalog entry they do not apply, and `skipped` is the expected outcome.

## Gate 3. Review and merge

Checks are green, but the merge is held by:

* `mergeable = MERGEABLE` — no conflicts with the base branch;
* `mergeStateStatus = BLOCKED` — waiting for a review;
* `reviewDecision` empty, no reviews or inline comments.

What the maintainer needs to do: look at the entry diff (three lines — `sha`, `version`, an extended
`description`) and submit a review. After that the merge button becomes available.

## What this means for our PR

1. We changed exactly three lines in `plugin-catalog/hermes-mobile.yaml`; no core code was touched.
2. The substantive part is the two catalog checks (`structural`, `pinned-source-validate`); the rest
   proves we broke nothing in the core.
3. Until the PR is merged the production pin does not move: `main` still carries `1.0.0 @ 7fdaf232…`,
   so installing the plugin by name gives 1.0.0 rather than our 1.1.1.
4. After the merge: update the plugin on already-installed agents — `hermes plugins update hermes-mobile`.

## Checking the state yourself

```bash
gh pr view 138527 --repo NousResearch/hermes-agent \
  --json state,mergeable,mergeStateStatus,reviewDecision,headRefOid
gh pr checks 138527 --repo NousResearch/hermes-agent          # checks of the head commit
gh api repos/NousResearch/hermes-agent/commits/<sha>/check-runs --jq '[.check_runs[].conclusion] | group_by(.) | map("\(length) × \(.[0])")'
```

Worth remembering: `gh pr checks` only lists checks that have already started. While runs wait for
approval the output is “no checks reported” — a state of waiting, not an empty requirement list.
