# ATR Report Rules

## Format
- KEY=VALUE lines only.
- No prose, no narrative, no Indonesian narration, no emoji.

## Required keys
SUCCESS, STAGE, PRE_HEAD, FINAL_COMMIT, REVIEW_REMOTE_HEAD,
OFFICIAL_REMOTE_HEAD, MAIN_REMOTE_HEAD, WORKTREE_CLEAN, LINEAR_HISTORY,
CHANGED_FILES, PROD_DB_BEFORE, PROD_DB_AFTER, PRIVATE_ARTIFACT_COMMITTED,
READY_FOR_INDEPENDENT_AUDIT, READY_FOR_PROMOTION, BLOCKER

## Defaults
- READY_FOR_PROMOTION=False unless owner approves.
- SUCCESS=False if any safety invariant fails.
- BLOCKER=NONE only when no issues.
