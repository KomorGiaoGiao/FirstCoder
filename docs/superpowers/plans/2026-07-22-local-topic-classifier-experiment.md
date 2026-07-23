# Local Topic Classifier Experiment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a fully local, reproducible pipeline that downloads XLM-RoBERTa-base, extracts and redacts FirstCoder and Codex conversations, prepares a 500-sample review set, and later fine-tunes and evaluates a `same/new` topic-boundary classifier without changing FirstCoder runtime behavior.

**Architecture:** Keep the experiment isolated under `benchmark/topic_classifier/`. Source adapters emit one normalized turn model; a deterministic redactor removes secrets and machine-specific identifiers; a candidate builder and grouped sampler create review artifacts under a Git-ignored local directory. Training consumes only human-confirmed labels, chooses an `uncertain` band on validation data, and writes reproducible metrics without importing the model into `firstcoder/`.

**Tech Stack:** Python 3.11+, pytest, JSONL, Hugging Face `transformers`/`huggingface_hub`, PyTorch MPS, scikit-learn.

---

## File map

- `benchmark/topic_classifier/models.py`: normalized turn, candidate, and manifest dataclasses.
- `benchmark/topic_classifier/firstcoder_adapter.py`: read-only FirstCoder JSONL parsing and boundary-label association.
- `benchmark/topic_classifier/codex_adapter.py`: read-only Codex rollout parsing and real-user-message extraction.
- `benchmark/topic_classifier/redaction.py`: deterministic secret and identifier redaction.
- `benchmark/topic_classifier/dataset.py`: context construction, stable IDs, quality flags, grouped sampling, and split validation.
- `benchmark/topic_classifier/io.py`: atomic JSONL/JSON writers and rejected-record manifests.
- `benchmark/topic_classifier/prepare.py`: dry-run and export CLI for the 500-sample review set.
- `benchmark/topic_classifier/download_model.py`: resumable local Hugging Face snapshot download.
- `benchmark/topic_classifier/train.py`: local XLM-R sequence-classification fine-tuning.
- `benchmark/topic_classifier/evaluate.py`: thresholds, metrics, latency, grouped reports, and redacted errors.
- `benchmark/topic_classifier/requirements.txt`: experiment-only dependencies; production dependencies remain unchanged.
- `benchmark/topic_classifier/README.md`: local commands, privacy guarantees, schemas, and artifact layout.
- `tests/topic_classifier/`: synthetic-fixture tests; tests never inspect real user logs.
- `.gitignore`: exclude local model, dataset, checkpoint, and report artifacts.

### Task 1: Establish the isolated experiment package and artifact boundary

**Files:**
- Create: `benchmark/topic_classifier/__init__.py`
- Create: `benchmark/topic_classifier/models.py`
- Create: `benchmark/topic_classifier/requirements.txt`
- Create: `benchmark/topic_classifier/README.md`
- Modify: `.gitignore`
- Test: `tests/topic_classifier/test_models.py`

- [ ] **Step 1: Write failing serialization tests**

Define tests that construct `NormalizedTurn`, `CandidateSample`, and `DatasetManifest`, round-trip each through `to_dict`/`from_dict`, reject blank IDs, reject unknown labels, and verify that raw source paths are not part of serialized candidate records.

- [ ] **Step 2: Run the focused test and confirm failure**

Run: `.venv/bin/python -m pytest tests/topic_classifier/test_models.py -q`

Expected: collection fails because `benchmark.topic_classifier.models` does not exist.

- [ ] **Step 3: Implement minimal typed models**

Use dataclasses and these exact public values:

```python
TopicLabel = Literal["same", "new", "uncertain"]
SourceName = Literal["firstcoder", "codex"]

@dataclass(frozen=True, slots=True)
class NormalizedTurn:
    source: SourceName
    source_session_id: str
    source_message_id: str
    created_at: str
    role: Literal["user", "assistant"]
    content: str
    weak_label: TopicLabel | None = None

@dataclass(frozen=True, slots=True)
class CandidateSample:
    sample_id: str
    source: SourceName
    session_group_id: str
    context: str
    new_message: str
    weak_label: TopicLabel | None
    human_label: TopicLabel | None
    confidence: float | None
    review_reason: tuple[str, ...]
    split: Literal["train", "validation", "test"] | None
```

Validate non-blank stable IDs and confidence in `[0, 1]`. `CandidateSample.to_dict()` must never include `source_session_id`, `source_message_id`, or a filesystem path.

- [ ] **Step 4: Add experiment dependencies and artifact documentation**

Pin only compatible lower bounds in `benchmark/topic_classifier/requirements.txt`:

```text
huggingface-hub>=0.33
transformers>=4.53
torch>=2.7
scikit-learn>=1.7
```

Document `.local/topic-classifier/{models,datasets,runs}` as the default output root and state that it is local-only.

- [ ] **Step 5: Ignore all local experiment artifacts**

Append exactly:

```gitignore
.local/topic-classifier/
```

- [ ] **Step 6: Run focused tests**

Run: `.venv/bin/python -m pytest tests/topic_classifier/test_models.py -q`

Expected: all tests pass.

- [ ] **Step 7: Commit**

```bash
git add .gitignore benchmark/topic_classifier tests/topic_classifier/test_models.py
git commit -m "Add topic classifier experiment scaffold"
```

### Task 2: Download a reproducible local XLM-R snapshot

**Files:**
- Create: `benchmark/topic_classifier/download_model.py`
- Test: `tests/topic_classifier/test_download_model.py`

- [ ] **Step 1: Write failing command-construction tests**

Test that defaults resolve to repository-local `.local/topic-classifier/models/xlm-roberta-base`, that `--revision` is recorded, and that an injected fake downloader receives `repo_id="FacebookAI/xlm-roberta-base"`, `local_dir`, and resumable download arguments.

- [ ] **Step 2: Run the test and confirm failure**

Run: `.venv/bin/python -m pytest tests/topic_classifier/test_download_model.py -q`

Expected: fails because `download_model.py` is absent.

- [ ] **Step 3: Implement download and manifest verification**

Expose:

```python
def download_model(
    *,
    output_dir: Path,
    revision: str = "main",
    snapshot_download_fn: Callable[..., str] = snapshot_download,
) -> Path:
    ...
```

After download, require `config.json`, tokenizer files, and at least one `*.safetensors` or `pytorch_model.bin`. Atomically write `download-manifest.json` containing repo ID, requested revision, resolved commit hash when available, file names, byte sizes, and completion time. A missing required file must produce a non-zero CLI exit.

- [ ] **Step 4: Run focused tests**

Run: `.venv/bin/python -m pytest tests/topic_classifier/test_download_model.py -q`

Expected: all tests pass without network access.

- [ ] **Step 5: Start the real download as a background job**

Install only experiment dependencies if imports are unavailable:

```bash
.venv/bin/python -m pip install -r benchmark/topic_classifier/requirements.txt
```

Start:

```bash
.venv/bin/python -m benchmark.topic_classifier.download_model \
  --output .local/topic-classifier/models/xlm-roberta-base \
  > .local/topic-classifier/model-download.log 2>&1
```

Run it through a persistent background process, record the PID, and do not treat launch as completion. Completion requires exit code `0`, a valid `download-manifest.json`, and all required files.

- [ ] **Step 6: Commit**

```bash
git add benchmark/topic_classifier/download_model.py tests/topic_classifier/test_download_model.py
git commit -m "Add local XLM-R snapshot downloader"
```

### Task 3: Parse FirstCoder logs without mutating them

**Files:**
- Create: `benchmark/topic_classifier/firstcoder_adapter.py`
- Test: `tests/topic_classifier/test_firstcoder_adapter.py`
- Test fixture: `tests/topic_classifier/fixtures/firstcoder_session.jsonl`

- [ ] **Step 1: Write failing adapter tests**

Use a fully synthetic session containing `session_created`, user/assistant messages, tool results, an implicit initial boundary, and later `same/new/uncertain` observations. Assert that:

- only user and assistant text turns are emitted;
- tool-result payloads are excluded;
- boundary observations join by `basis_message_id`;
- implicit-initial-task observations do not become training labels;
- invalid JSON produces a structured rejection with hashed path and line number;
- source files remain byte-identical before and after parsing.

- [ ] **Step 2: Run the test and confirm failure**

Run: `.venv/bin/python -m pytest tests/topic_classifier/test_firstcoder_adapter.py -q`

Expected: fails because the adapter does not exist.

- [ ] **Step 3: Implement streaming parsing**

Expose:

```python
def iter_firstcoder_session(path: Path) -> Iterator[NormalizedTurn]:
    ...

def scan_firstcoder(root: Path) -> ScanResult:
    ...
```

Read line-by-line. Associate `task_boundary_observed.payload.decision` to its `basis_message_id`, except when `confirmation_reason` is `implicit_initial_task` or `initial_task`. Never instantiate `JsonlSessionStore`, because its constructor creates directories; this adapter must remain read-only.

- [ ] **Step 4: Run focused tests**

Run: `.venv/bin/python -m pytest tests/topic_classifier/test_firstcoder_adapter.py -q`

Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add benchmark/topic_classifier/firstcoder_adapter.py tests/topic_classifier
git commit -m "Parse FirstCoder topic training logs"
```

### Task 4: Parse Codex rollouts and isolate real user messages

**Files:**
- Create: `benchmark/topic_classifier/codex_adapter.py`
- Test: `tests/topic_classifier/test_codex_adapter.py`
- Test fixture: `tests/topic_classifier/fixtures/codex_rollout.jsonl`

- [ ] **Step 1: Write failing Codex parsing tests**

The synthetic rollout must include `session_meta`, `turn_context`, developer messages, duplicated `response_item` user content, `event_msg.user_message`, agent commentary/final messages, function calls, tool outputs, and a steered message. Assert that only canonical `event_msg` user messages and visible assistant text are emitted, duplicate representations are removed, developer/system instructions are excluded, and `session_meta.payload.id` forms the source session ID.

- [ ] **Step 2: Run the test and confirm failure**

Run: `.venv/bin/python -m pytest tests/topic_classifier/test_codex_adapter.py -q`

Expected: fails because the adapter does not exist.

- [ ] **Step 3: Implement streaming event normalization**

Expose `iter_codex_rollout(path: Path) -> Iterator[NormalizedTurn]` and `scan_codex(root: Path) -> ScanResult`. Prefer `event_msg.payload.type == "user_message"` for canonical user text. Accept visible assistant text only from `event_msg.payload.type == "agent_message"`; exclude reasoning, summaries, tool calls, developer messages, and token counts. Create deterministic synthetic message IDs from session ID, timestamp, event index, and content hash.

- [ ] **Step 4: Run focused tests**

Run: `.venv/bin/python -m pytest tests/topic_classifier/test_codex_adapter.py -q`

Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add benchmark/topic_classifier/codex_adapter.py tests/topic_classifier
git commit -m "Parse Codex topic training logs"
```

### Task 5: Deterministically redact secrets and machine identifiers

**Files:**
- Create: `benchmark/topic_classifier/redaction.py`
- Test: `tests/topic_classifier/test_redaction.py`

- [ ] **Step 1: Write failing redaction tests**

Cover OpenAI/Anthropic-style tokens, bearer headers, cookies, private-key blocks, `.env` assignments, emails, phone numbers, IPv4 addresses, `/Users/<name>` paths, query-string secrets, and ordinary code that must remain unchanged. Assert idempotence: `redact(redact(text).text).text == redact(text).text`.

- [ ] **Step 2: Run the test and confirm failure**

Run: `.venv/bin/python -m pytest tests/topic_classifier/test_redaction.py -q`

Expected: fails because `redaction.py` does not exist.

- [ ] **Step 3: Implement ordered redaction rules**

Return a `RedactionResult(text, hits)` with stable placeholders such as `<SECRET>`, `<EMAIL>`, `<PHONE>`, `<IP>`, `<HOME>`, and `<PRIVATE_KEY>`. Apply private-key and header rules before generic token rules. Never include matched secret text in `hits`; store only rule name and count.

- [ ] **Step 4: Add a conservative rejection signal**

Expose `contains_unredacted_secret(text) -> bool` for high-confidence residual patterns. Candidate construction must reject, rather than export, any record where this returns true.

- [ ] **Step 5: Run focused tests**

Run: `.venv/bin/python -m pytest tests/topic_classifier/test_redaction.py -q`

Expected: all tests pass.

- [ ] **Step 6: Commit**

```bash
git add benchmark/topic_classifier/redaction.py tests/topic_classifier/test_redaction.py
git commit -m "Redact local topic dataset text"
```

### Task 6: Build context pairs, stable IDs, and quality flags

**Files:**
- Create: `benchmark/topic_classifier/dataset.py`
- Test: `tests/topic_classifier/test_dataset.py`

- [ ] **Step 1: Write failing candidate-builder tests**

Assert that the first user message in each session is excluded; each later user message becomes one candidate; context contains only prior turns; the newest context is preserved when the character budget is exceeded; raw IDs are HMAC-hashed with a local dataset salt; weak labels propagate only to the matching user turn; and short/reference/switch markers produce quality flags without deciding the human label.

- [ ] **Step 2: Run the test and confirm failure**

Run: `.venv/bin/python -m pytest tests/topic_classifier/test_dataset.py -q`

Expected: fails because candidate construction is absent.

- [ ] **Step 3: Implement candidate construction**

Expose:

```python
def build_candidates(
    turns: Iterable[NormalizedTurn],
    *,
    dataset_salt: bytes,
    max_context_chars: int = 8_000,
) -> CandidateBuildResult:
    ...
```

Format context with explicit `[USER]` and `[ASSISTANT]` separators. Run redaction before serialization. Flags include `short_message`, `anaphora`, `explicit_switch_phrase`, `weak_label_uncertain`, `no_assistant_context`, and `source_codex`. Do not convert flags into ground-truth labels.

- [ ] **Step 4: Implement grouped split validation**

Reject any assignment where one `session_group_id` appears in more than one split. Reserve exactly 100 fully reviewed records for test; keep the rest in train/validation groups.

- [ ] **Step 5: Run focused tests**

Run: `.venv/bin/python -m pytest tests/topic_classifier/test_dataset.py -q`

Expected: all tests pass.

- [ ] **Step 6: Commit**

```bash
git add benchmark/topic_classifier/dataset.py tests/topic_classifier/test_dataset.py
git commit -m "Build redacted topic boundary candidates"
```

### Task 7: Sample and export the 500-record review dataset

**Files:**
- Create: `benchmark/topic_classifier/io.py`
- Create: `benchmark/topic_classifier/prepare.py`
- Test: `tests/topic_classifier/test_prepare.py`

- [ ] **Step 1: Write failing dry-run and export tests**

Inject temporary FirstCoder/Codex roots. Assert default dry-run writes nothing, reports per-source counts and rejection categories, and never prints message text. Assert explicit `--output` atomically writes `candidates.jsonl`, `review.csv`, `manifest.json`, and `rejections.jsonl`; interrupted writes leave no final partial file.

- [ ] **Step 2: Run the test and confirm failure**

Run: `.venv/bin/python -m pytest tests/topic_classifier/test_prepare.py -q`

Expected: fails because the prepare CLI is absent.

- [ ] **Step 3: Implement deterministic stratified sampling**

Use a fixed seed and quotas across source, weak-label bucket, message length, and difficulty flags. Deduplicate on normalized `(context_tail, new_message)` hashes. If a quota lacks candidates, redistribute deterministically and record the shortfall in the manifest. Never invent `human_label`.

- [ ] **Step 4: Implement review export**

`review.csv` columns are `sample_id`, `source`, `context`, `new_message`, `suggested_label`, `human_label`, `review_status`, `confidence`, `review_reason`, and `notes`. `suggested_label` may use FirstCoder weak labels or conservative rules; `human_label` remains blank until reviewed. Mark all low-confidence/conflicting rows for review and randomly mark a fixed high-confidence audit subset.

- [ ] **Step 5: Run focused tests and privacy scan**

Run:

```bash
.venv/bin/python -m pytest tests/topic_classifier/test_prepare.py -q
.venv/bin/python -m pytest tests/topic_classifier -q
```

Expected: all tests pass; synthetic secrets never appear in exported fixtures.

- [ ] **Step 6: Run a real dry-run**

```bash
.venv/bin/python -m benchmark.topic_classifier.prepare \
  --firstcoder-root /Users/x/.firstcoder/sessions \
  --codex-root /Users/x/.codex/sessions
```

Expected: prints counts and rejection reasons only; writes no files.

- [ ] **Step 7: Export the local 500-sample review set**

```bash
.venv/bin/python -m benchmark.topic_classifier.prepare \
  --firstcoder-root /Users/x/.firstcoder/sessions \
  --codex-root /Users/x/.codex/sessions \
  --output .local/topic-classifier/datasets/v1 \
  --sample-size 500 \
  --seed 20260722
```

Expected: 500 rows, no residual-secret findings, 100 test candidates reserved by session group, and a manifest containing source counts, hashes, and rejection totals.

- [ ] **Step 8: Commit**

```bash
git add benchmark/topic_classifier/io.py benchmark/topic_classifier/prepare.py tests/topic_classifier
git commit -m "Prepare local topic review dataset"
```

### Task 8: Complete local review and freeze dataset version 1

**Files:**
- Local only: `.local/topic-classifier/datasets/v1/review.csv`
- Local only: `.local/topic-classifier/datasets/v1/labeled.jsonl`
- Local only: `.local/topic-classifier/datasets/v1/label-manifest.json`
- Modify: `benchmark/topic_classifier/prepare.py`
- Test: `tests/topic_classifier/test_label_validation.py`

- [ ] **Step 1: Write failing label-validation tests**

Require every test row to have a human label, prohibit `uncertain` from entering binary training rows, retain uncertain rows in an analysis file, reject duplicate sample IDs, and reject labels not in `same/new/uncertain`.

- [ ] **Step 2: Implement `validate-labels` and `freeze` subcommands**

`validate-labels` reports missing/invalid labels without showing text. `freeze` writes immutable labeled and uncertain JSONL files plus SHA-256 hashes and exact per-split/per-label counts.

- [ ] **Step 3: Run tests**

Run: `.venv/bin/python -m pytest tests/topic_classifier/test_label_validation.py -q`

Expected: all tests pass.

- [ ] **Step 4: Review low-confidence records and audit high-confidence records**

Complete `human_label` for every test candidate and every row marked `needs_review`; correct suggested labels where full context indicates a different goal/state relationship. Do not accept a suggested label solely because it matches the existing FirstCoder weak label.

- [ ] **Step 5: Freeze dataset v1**

Run:

```bash
.venv/bin/python -m benchmark.topic_classifier.prepare validate-labels \
  --review .local/topic-classifier/datasets/v1/review.csv
.venv/bin/python -m benchmark.topic_classifier.prepare freeze \
  --review .local/topic-classifier/datasets/v1/review.csv \
  --output .local/topic-classifier/datasets/v1
```

Expected: validation succeeds and manifest hashes are stable across a repeated freeze.

- [ ] **Step 6: Commit code only**

```bash
git add benchmark/topic_classifier/prepare.py tests/topic_classifier/test_label_validation.py
git commit -m "Validate topic classifier labels"
```

### Task 9: Fine-tune XLM-R locally with session-grouped data

**Files:**
- Create: `benchmark/topic_classifier/train.py`
- Test: `tests/topic_classifier/test_train.py`

- [ ] **Step 1: Write failing training-configuration tests**

Test label mapping (`same=0`, `new=1`), paired input formatting, 512-token truncation, session-group split enforcement, deterministic seed setup, MPS/CPU selection, and refusal to train when any test row lacks a human label.

- [ ] **Step 2: Implement lazy ML imports and configuration**

Keep `torch` and `transformers` imports inside training entry points so dataset preparation works without ML dependencies. Default to local-files-only model loading after snapshot download. Write `run-config.json` before training with dataset and model manifest hashes.

- [ ] **Step 3: Implement conservative M1 defaults**

Use maximum length 512, per-device batch size 2, gradient accumulation 8, three epochs, learning rate `2e-5`, weight decay `0.01`, fixed seed `20260722`, best-checkpoint selection by validation `new` recall with macro-F1 as a secondary metric, and early stopping. Allow CLI overrides while recording them.

- [ ] **Step 4: Run unit tests**

Run: `.venv/bin/python -m pytest tests/topic_classifier/test_train.py -q`

Expected: all tests pass with fake tokenizer/model objects and no model download.

- [ ] **Step 5: Run a one-batch smoke test**

Use a tiny synthetic labeled fixture, CPU, and a fake or tiny local model fixture. Confirm checkpoint/report layout without consuming the real test set.

- [ ] **Step 6: Run real local training**

```bash
.venv/bin/python -m benchmark.topic_classifier.train \
  --dataset .local/topic-classifier/datasets/v1/labeled.jsonl \
  --model .local/topic-classifier/models/xlm-roberta-base \
  --output .local/topic-classifier/runs/v1
```

Expected: successful completion, best checkpoint, trainer state, run config, and validation predictions.

- [ ] **Step 7: Commit**

```bash
git add benchmark/topic_classifier/train.py tests/topic_classifier/test_train.py
git commit -m "Train local topic boundary classifier"
```

### Task 10: Tune the uncertain band and generate the final benchmark report

**Files:**
- Create: `benchmark/topic_classifier/evaluate.py`
- Test: `tests/topic_classifier/test_evaluate.py`

- [ ] **Step 1: Write failing threshold and metric tests**

Use fixed probabilities to assert confusion-matrix values, `new` precision/recall/F1, macro-F1, uncertain coverage, covered accuracy, grouped metrics, and deterministic threshold selection that minimizes `new -> same` errors before optimizing coverage.

- [ ] **Step 2: Implement validation-only threshold selection**

Search separate same/new probability thresholds on validation predictions only. Never tune on the test set. Persist selected thresholds and the objective ordering.

- [ ] **Step 3: Implement test evaluation and latency measurement**

Report JSON and Markdown with overall/source/length/difficulty metrics, baseline comparisons, confusion matrix, uncertain coverage, batch and single-record latency, and redacted error examples keyed by `sample_id`.

- [ ] **Step 4: Run focused and full tests**

Run:

```bash
.venv/bin/python -m pytest tests/topic_classifier/test_evaluate.py -q
.venv/bin/python -m pytest tests/topic_classifier -q
.venv/bin/python -m pytest
```

Expected: all new tests pass; full-suite result is recorded with any pre-existing unrelated failures explicitly separated.

- [ ] **Step 5: Generate the final report**

```bash
.venv/bin/python -m benchmark.topic_classifier.evaluate \
  --run .local/topic-classifier/runs/v1 \
  --dataset .local/topic-classifier/datasets/v1/labeled.jsonl \
  --output .local/topic-classifier/runs/v1/report
```

Expected: `metrics.json`, `report.md`, `predictions.jsonl`, and `errors.jsonl`, with the test split evaluated exactly once after threshold selection.

- [ ] **Step 6: Verify runtime isolation**

Run:

```bash
git diff -- firstcoder/
git status --short
```

Expected: no experiment changes under `firstcoder/`; no model, raw log, dataset, or checkpoint is tracked by Git.

- [ ] **Step 7: Commit**

```bash
git add benchmark/topic_classifier/evaluate.py tests/topic_classifier/test_evaluate.py benchmark/topic_classifier/README.md
git commit -m "Evaluate local topic boundary classifier"
```
