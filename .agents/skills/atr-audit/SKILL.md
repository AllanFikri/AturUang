---
name: atr-audit
description: Audit an Antigravity stage report by verifying against actual Git diff, tests, and DB hashes. Returns ACCEPTED / REPAIR REQUIRED / REJECTED. Use after every stage completion.
---

# AturUang Report Audit

## Principle
Never trust report claims. Verify against actual state.

## Steps
1. Match every prompt item to a report key.
2. git --no-pager diff <pre_head> <final_commit> --stat. Confirm CHANGED_FILES.
3. Diff touches only SCOPE files.
4. Locate test run output. Confirm production code invoked, not copies.
5. PROD_DB_BEFORE == PROD_DB_AFTER == canonical hash.
6. Check diff for secrets.json/PDF/ZIP/CSV/DB.
7. git merge-base --is-ancestor <baseline> <final> == true.
8. git ls-remote origin <branch> == FINAL_COMMIT.

## Output
AUDIT_RESULT=ACCEPTED|REPAIR REQUIRED|REJECTED|PROMOTION ACCEPTED
AUDIT_NOTES=<comma-separated or NONE>
DIFF_MATCH=True/False
SCOPE_MATCH=True/False
TESTS_VERIFIED=True/False
PROD_DB_MATCH=True/False
PRIVATE_ARTIFACTS=False
LINEAR_HISTORY=True/False
