# ATR Git Rules

## Branches
- Review: review/<stage>-v1, linear descendant of baseline.
- Integration: feature/universal-ingestion-v1.
- Release: main.
- Current baseline: 494d78f33f4cd91c1434c3c9ea2b72fb6e0ecfaf.

## Commits
- One commit per stage or per repair.
- Format: <type>(<scope>): <message>.
- No secrets.json / PDF / ZIP / CSV / DB.
- No emoji.

## Promotion
- Fast-forward only. Never merge/squash/rebase/amend.
- Command: git push origin <sha>:refs/heads/<target>.
- Owner approves promotion.
