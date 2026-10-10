# Demo script (10 minutes, mock mode)

Audience: leadership and client stakeholders. Runs offline: no cloud
accounts, no credentials, no LLM key. Rehearse once; a full run takes under a
minute of machine time.

## Before the demo

```bash
cd stackit-migration-agents
uv sync
uv run migrate run --source aws --mock --yes > /dev/null   # warm-up, checks everything works
```

Use a wide terminal (≥ 140 columns) and open a Markdown viewer next to it for
the reports.

## 1. The problem (1 min)

> "Moving to a sovereign German cloud is mostly analysis, translation and
> verification work. We built agents that do that work, with humans approving
> every change."

Show the architecture diagram in the README.

## 2. Discovery (2 min)

```bash
uv run migrate discover --source aws --mock
```

Point at:
- The inventory runs with **describe/list calls only**. The AWS adapter rejects
  anything else in code, and the IAM policy is in `docs/aws-readonly-policy.json`.
- The mapping table: most of the shop maps cleanly (servers, PostgreSQL Flex,
  Object Storage, SKE).
- **Honest gaps:** the Windows server (licensing), MySQL (STACKIT has no
  managed MySQL, so it maps to MariaDB with review), Kubernetes 1.29 → 1.30.
- **The LLM adds judgement:** it noticed RDP open to the internet and that the
  main database has three consumers, which matters at cutover. Rules alone
  would not catch these.
- Open `runs/<id>/discovery-report.md`. The cost section says "price not set":
  we never invent prices; `config/pricing.yaml` is filled from the official
  price lists.

## 3. Deployment (2 min)

```bash
uv run migrate deploy <run-id> --mode apply
```

Point at:
- 39 STACKIT resources generated as Terraform from **templates**, not by the
  LLM. Open `runs/<id>/terraform/compute.tf`: readable, reviewable,
  `REVIEW:` comments where a human must decide.
- The LLM explains the plan for the approver (what, risks, what is left out).
- **Nothing happens until a human approves.** The approval is bound to the
  exact plan file: if anyone changes it afterwards, apply is refused.

```bash
uv run migrate approve <run-id>
```

Mention PR mode: `--mode pr` opens a pull request instead, so the client's own
pipeline applies it.

## 4. Data migration (2 min)

```bash
uv run migrate data <run-id>
```

Point at:
- rclone `copy` and pg_dump/pg_restore. **Source data is never deleted**, and
  the code refuses `sync`/`move`/`delete`.
- Verification: object counts, sizes, checksums on a sample, row counts per
  table, schema diff. 8/8 green.
- Skipped with reasons: archived (Glacier) logs and the MySQL copy.
- Second human gate for the cutover, plus a generated runbook with a rollback
  plan.

```bash
uv run migrate approve <run-id>
```

## 5. Monitoring (1 min)

```bash
uv run migrate monitor <run-id> --iterations 1
```

Point at: two anomalies after the move. The LLM investigated with read-only
queries and proposes fixes. **It cannot apply them.**

## 6. Trust and sovereignty (2 min)

```bash
uv run migrate status <run-id>
head -n 5 runs/<run-id>/audit.jsonl
```

- Every tool call, LLM decision and approval is in the audit log, with secrets
  redacted.
- The LLM can run on **STACKIT AI Model Serving** by changing three settings
  (README, "Switching the LLM"): the whole loop stays in a German cloud.
- Runs survive restarts (checkpointed); `migrate resume` continues.

## Likely questions

| Question | Answer |
|---|---|
| Does the AI change our cloud? | No. It never gets a mutating tool. Changes run from templates, after a human approval bound to the exact plan. |
| What about our data? | Copied, never deleted, verified with counts, checksums and row counts before the cutover approval. |
| Which LLM? | Any OpenAI-compatible model, including STACKIT AI Model Serving. Configuration only. |
| How do you know the Terraform is right? | It uses the official provider; CI runs `terraform validate` on it; uncertain parts are marked `REVIEW`/`TODO(verify)`. |
| Azure? | Same interface; the Azure adapter is a stub today and comes next. |

## Live variant

`demo/aws-sample-app` creates a real small source app on AWS (EC2, RDS, S3)
with a teardown script. Use it only with a prepared STACKIT project; see that
directory's README.
