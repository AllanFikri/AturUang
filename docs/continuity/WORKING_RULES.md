# AturUang Working Rules

These rules describe how the user, ChatGPT, and coding agents should work
together on this project.

## Communication

Use simple, direct Indonesian.

Start with the conclusion when possible.

Explain enough of the reason so a non-expert can understand why a command or
decision is being made.

Avoid:
- empty praise;
- excessive headings;
- repeated reassurance;
- claiming PASS without evidence;
- jargon without explanation.

For binary questions, answer Yes/No first when practical.

## Technical-lead behavior

Instructions to coding agents should be:
- scoped;
- specific;
- evidence-based;
- explicit about prohibitions;
- explicit about acceptance criteria;
- explicit about tests;
- explicit about stop conditions;
- explicit about commit expectations.

Do not issue vague requests such as "fix everything."

One stage should be completed and audited before moving to the next stage.

## Primary workspace

Use VS Code and the canonical GitHub workspace.

Prefer terminal commands that are short and understandable.

A larger automation block is acceptable when it performs one coherent,
well-guarded task.

Do not edit the obsolete Downloads workspace.

## Clipboard workflow

Whenever terminal output needs to be returned to ChatGPT, automatically place
the useful result into the clipboard with Set-Clipboard.

A failure clipboard should include:
- compact summary;
- actual error;
- relevant stdout/stderr;
- failed command/context when useful.

The user should not have to manually select long terminal output.

## Long-running commands

Before a command likely to take time, print a visible line beginning with:

LOADING:

This distinguishes real execution from a PowerShell continuation prompt.

If PowerShell displays:
>>

that normally means the parser is waiting for more input, not that the task is
loading.

Use Ctrl+C and correct the command rather than waiting indefinitely.

## Failure protocol

Do not patch speculatively when evidence can be collected first.

Preferred sequence:
1. reproduce/observe;
2. obtain the exact error code;
3. narrow the failing layer;
4. make the smallest justified correction;
5. rerun the targeted validation;
6. only then commit/deploy.

A failed test is evidence, not permission to weaken an assertion.

## Testing discipline

Use targeted tests during implementation.

Use:
python tests/run_all.py quick
for broad sanity when appropriate.

Use:
python tests/run_all.py full
at meaningful release/final gates, not after every documentation change.

Also use relevant:
- python -m compileall -q .
- git diff --check
- TypeScript tests/checks for cloud changes

Do not repeatedly run expensive cloud parity suites without a reason.

Mutation tests must use temporary/in-memory data.

## SQLite safety

Production SQLite is financial state.

Never mutate it from tests, backfill experiments, or cloud-ingestion smoke
tests.

For high-risk work, compare actual before/after SHA-256.

Current known production hash as of 2026-08-26:
2b537bbcaa6a22bbd7018630b84152a319ce352624b97f5ace41563c1561945f

Do not assume a historical hash is universally correct after legitimate user
transactions. Always verify current state when needed.

## Cloud/D1 safety

MODE remains shadow until an explicit controlled cutover.

Do not:
- open production ledger write routes early;
- dual-write;
- delete broad D1 ranges to solve a local anomaly;
- interpret a Cloudflare warning as permission for a major dependency upgrade.

Destructive repair requires:
- exact target evidence;
- narrow guard;
- read-only precheck;
- post-repair audit.

## Gmail safety

Gmail is read-only.

Never:
- mark messages read;
- archive;
- delete;
- label;
- send;
- forward;
- mutate mailbox state.

Historical backfill should be controlled and resumable.

Do not run the next month until the prior month has completed and its exact
audit has passed.

## Secrets and privacy

Never ask the user to paste secrets into chat.

Never put secrets in:
- Git;
- logs;
- clipboard diagnostics;
- screenshots intended for sharing;
- test fixtures.

Examples include:
- Cloudflare API tokens;
- staging admin tokens;
- Gmail relay shared secret;
- Telegram bot token;
- recovery credentials;
- .env/.dev.vars contents.

Do not log:
- raw email bodies;
- attachments;
- full account numbers;
- unnecessary PII;
- production transaction exports.

Use official secret stores:
- Wrangler/Cloudflare Secrets;
- Apps Script Properties;
- GitHub encrypted secrets where appropriate;
- BotFather/Telegram official setup.

## Git discipline

Before an implementation:
- inspect git status.

After changes:
- run relevant checks;
- inspect intended file scope;
- run git diff --check.

Do not silently commit/push/deploy.

When automation will commit/push/deploy, tell the user beforehand.

Commit messages should describe one coherent change.

Keep working tree clean at milestone boundaries.

## Documentation discipline

Every meaningful milestone must be recorded.

After implementation, deployment, operational repair, or material audit:

WORKLOG.md:
append what happened, including failures and corrections.

HANDOFF.md:
update current HEAD/deployment/schema/checkpoint/blocker/next action.

DECISIONS.md:
add/update ADR only when a durable architectural/product decision changed.

WORKING_RULES.md:
change only when collaboration/process rules changed.

Do not rewrite history to hide failures.
Failures and false starts are useful continuity evidence.

Do not invent timestamps, commit IDs, test results, or causes.
If uncertain, mark the information as reconstructed or unknown.

## Handoff to another GPT

A new GPT should not start by proposing a redesign.

It should:
1. read AGENTS.md;
2. read HANDOFF.md;
3. read WORKING_RULES.md;
4. read DECISIONS.md;
5. inspect the latest WORKLOG entries;
6. verify actual repo/cloud/database state relevant to the next task;
7. continue from the documented next action.

Repository and runtime evidence always beat stale handoff text.
