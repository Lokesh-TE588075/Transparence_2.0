# Change Control Rules

## TransparencE Statefix Refactoring — All Phases

These rules apply to every implementation chat from Phase 1 onward.

---

## Rule 1: One file per implementation chat

Only **one existing production module** may be modified per implementation chat session.
New independent modules may be created in the same session as they have no prior state to corrupt.

## Rule 2: Analysis and implementation in separate chats

Analysis sessions (reading files, planning, writing design documents) must be completed
before implementation begins. Do not mix analysis decisions with code edits in the same chat.

## Rule 3: Clean working tree before each phase

Every implementation session must begin from a clean working tree.
Before the first edit, verify:
- The `SOURCE_INTEGRITY_BASELINE.json` manifest (or Git `git status`) shows no uncommitted changes.
- The targeted test baseline passes without modification.

## Rule 4: Every implementation ends with a complete verification sequence

No implementation session is complete until all of the following pass:

1. `ast.parse` for every modified Python file.
2. Targeted tests (`pytest -q <relevant test files>`).
3. Non-live regression tests when the change touches shared infrastructure.
4. `git diff --check` (no whitespace errors, no conflict markers).
5. Reviewed `git diff` (confirm only intended lines changed).
6. Git commit with a descriptive message.

## Rule 5: No deployment in an implementation chat

Deployment is a separate step performed in a separate, dedicated chat session
after all tests pass and the diff is reviewed. Never deploy mid-implementation.

## Rule 6: No large complete-file rewrites without explicit approval

Large complete-file rewrites are prohibited unless the user has explicitly approved them
and the full new file content has been reviewed in the same session.

## Rule 7: Abort immediately on corruption signals

Any mention of the following **requires the phase to stop immediately**:

- split-write
- chunk reconstruction
- mixed old/new code
- anchor mismatch
- file corruption
- timed-out partial patch
- incomplete edit

Stop, restore from the backup, verify the baseline, and document what happened
before reopening an implementation session.

## Rule 8: No direct production workspace edits

No future phase may modify the production workspace directly.
All development happens in `transparence_app_statefix/`.
The production workspace is read-only during refactoring.

## Rule 9: The active deployed snapshot is the emergency recovery source

If the development copy becomes corrupted beyond repair, the active deployed snapshot at:
```
/Workspace/Users/488a0acb-5804-42f0-98b1-a02cc13c4573/src/01f181110277111f8f8d22379e477ecc
```
is always the authoritative recovery source.
The Phase 0 backup at `baseline_backups/statefix_phase0_20260718T073833Z/` is the secondary recovery source.

## Rule 10: Session boundary disciplines

| At start of session | At end of session |
|---|---|
| Verify active deployment ID unchanged | All modified files pass ast.parse |
| Run targeted test baseline | Targeted tests pass |
| Confirm clean working tree | Commit created (or manifest updated) |
| Note which module will be changed | No partial patches left open |
| Do not read from production workspace | Production workspace unmodified |

---

## Absolute Prohibitions (apply to all phases)

- Do not modify `app/services/genie_pipeline.py` without explicit per-phase approval.
- Do not modify `app/services/genie_session_store.py` without explicit per-phase approval.
- Do not modify `app/routes/chat.py` without explicit per-phase approval.
- Do not modify frontend source without explicit per-phase approval.
- Do not modify `app.yaml`.
- Do not modify feature flags without explicit approval.
- Do not initialize or modify Lakebase resources.
- Do not create database tables outside of an approved schema migration step.
- Do not change Genie configuration.
- Do not deploy, restart, stop, or create a new app deployment in an implementation chat.
