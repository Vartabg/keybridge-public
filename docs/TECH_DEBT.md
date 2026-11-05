# Technical debt

## 2026-10-03 — Provisioning module size

`keybridge/deployment.py` is 257 lines, above the shared 200-line guideline.
It contains the validated Vercel adapter, private receipt/recovery helpers and
owner setup lock. A later maintenance task can split those responsibilities
while retaining the existing uncertain-write, scope and locking regressions.
The current bounded review found no blocking correctness or security defect;
avoid restructuring credential-handling code during this integration release.
