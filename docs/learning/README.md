# Learning Documentation

This directory is the engineering history of the trading system. It is separate
from `docs/engineering/`, and the split is deliberate.

| Directory | Question it answers | Rewrite policy |
|---|---|---|
| `docs/engineering/` | What does the system do today? | Rewritten freely whenever the system changes |
| `docs/learning/` | How did it come to work this way, and what did we learn? | Append-only for history; current-state sections updated in place |

If you only need to operate or modify the system, read `AGENTS.md` and
`docs/engineering/` first. Come here when you need to know *why* something is
built the way it is, or when you are about to change something that looks
unnecessarily defensive — usually it is load-bearing, and the case study
explains what happened the last time it was not there.

## How to read it

Start at [`SYSTEM_ENGINEERING_LEARNING_LOG.md`](SYSTEM_ENGINEERING_LEARNING_LOG.md).
It is the table of contents; the chapters live in separate files so this stays
maintainable for years rather than becoming one unreadable file.

Every Part I and Part II chapter is split into two sections, and the split
matters:

- **Current Implementation** — how it works today. Update this when the code
  changes.
- **Engineering History** — what happened, in order. Do not rewrite this when
  the code changes, and never rewrite it to make a past investigation look
  smarter than it was.

## Sourcing rules

These documents are written from material in this repository. Every factual
claim should be traceable to one of:

- source code at a named path
- an existing document under `docs/`
- an Alembic migration under `backend/alembic/versions/`
- a test under `backend/tests/` (regression tests frequently document the exact
  defect they were written for, in their module docstring)
- the git history

**Confidence must be labelled.** The review reports under `docs/review/` set the
convention this documentation follows, and it is worth keeping:

| Label | Meaning |
|---|---|
| **Confirmed** | Directly verifiable in code, schema, tests or git history today |
| **Reported** | Asserted by a dated document in the repository, not independently re-verified here |
| **Assumption** | A stated premise the analysis depends on, not itself proven |
| **Unknown** | Explicitly not established by available material |

When you cannot establish something, write "not established by available
material" and move on. An honest gap is more useful than a plausible
invention — a future reader cannot tell the difference between a confident
guess and a fact, and will act on both.

## Adding a case study

Use the template in
[`part3_engineering_history/15_major_production_issues.md`](part3_engineering_history/15_major_production_issues.md).

Rules that exist for a reason:

1. **Preserve the wrong turns.** Section 3 ("Initial Understanding") exists so a
   reader learns how the problem was actually diagnosed. If the first theory was
   wrong, that is the most valuable paragraph in the document. Do not delete it
   once the answer is known.
2. **Do not upgrade confidence retroactively.** If something was "likely" when
   written, it stays "likely" unless someone re-verifies it and says so.
3. **A fix that was reverted is still history.** Record it, and record why it
   was reverted.
4. **Cite the commit.** A short SHA and a date make the claim checkable.
5. **Do not claim a bug existed** unless the material establishes it. "This code
   looks risky" is a limitation for Chapter 21, not a case study.

## Status of this documentation

This set was assembled on 2026-09-21 from the repository as it stood at that
date. It is not complete: 224 commits and roughly 4,700 lines of prior review
material cannot all be turned into case studies in one pass. Chapters state
what they cover and what they do not. Gaps are marked rather than filled with
guesses.
