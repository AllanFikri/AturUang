---
name: atr-preflight
description: Verify AturUang baseline before starting any stage. Stops on any mismatch with the expected integration head, production DB hash, or worktree state.
---

# AturUang Preflight

## Checks
1. git rev-parse HEAD == expected integration head (default 494d78f...).
2. Get-FileHash production DB == 8afc9582...
3. git status --porcelain == empty.
4. git merge-base --is-ancestor <baseline> HEAD == true.
5. git worktree list count == 3.

## Output
PREFLIGHT_RESULT=PASS|FAIL
FAILURE_ITEMS=<comma-separated or NONE>
INTEGRATION_HEAD=<sha>
PROD_DB_HASH=<sha>
WORKTREE_CLEAN=True/False

## On fail
Report FAILURE_ITEMS. Do not proceed. Do not auto-fix.
