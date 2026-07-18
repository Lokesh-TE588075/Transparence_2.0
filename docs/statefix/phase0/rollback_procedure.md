# Rollback Procedure

## Phase 0 — TransparencE Statefix Refactoring

---

## Overview

This document explains how to discard a failed implementation phase, restore the development
branch to the Phase 0 baseline, and recover if any file was partially modified.

The running app is **never touched** by these procedures. The production workspace is
**never touched**. All rollback operations act only on the development copy.

---

## 1. How to discard an incomplete phase

If a phase implementation is incomplete or corrupted, stop immediately and:

### Option A: Restore from the baseline backup (preferred — most reliable)

```python
import shutil, os

DEV = "/Workspace/Users/lokesh.choraria@te.com/Transparence/transparence_app_statefix"
BACKUP = "/Workspace/Users/lokesh.choraria@te.com/Transparence/baseline_backups/statefix_phase0_20260718T073833Z"

# For a specific file:
shutil.copy2(os.path.join(BACKUP, "app/services/genie_pipeline.py"),
             os.path.join(DEV,    "app/services/genie_pipeline.py"))

# For the entire dev copy (nuclear option — erases all Phase N work):
import glob
# Do not delete Phase 0 docs. Restore only app/, tests/, static/, frontend/, config files.
```

### Option B: If Git is configured

```bash
# Discard all uncommitted changes:
git -C <dev_path> checkout .

# Reset to baseline commit (replace <sha> with the commit SHA from version_control_status.md):
git -C <dev_path> reset --hard <baseline_sha>
```

### Option C: Create a timestamped alternative dev copy

```python
import shutil
shutil.copytree(
    "/Workspace/Users/lokesh.choraria@te.com/Transparence/baseline_backups/statefix_phase0_20260718T073833Z",
    f"/Workspace/Users/lokesh.choraria@te.com/Transparence/transparence_app_statefix_recovery_{TIMESTAMP}",
    ignore=shutil.ignore_patterns("MANIFEST.json")
)
```

---

## 2. How to verify that the development copy matches the Phase 0 baseline

Run this in a Databricks notebook or `executeCode` cell:

```python
import os, hashlib, json

DEV = "/Workspace/Users/lokesh.choraria@te.com/Transparence/transparence_app_statefix"
MANIFEST = os.path.join(DEV, "SOURCE_INTEGRITY_BASELINE.json")

with open(MANIFEST) as f:
    manifest = json.load(f)

changed = []
for entry in manifest["files"]:
    path = os.path.join(DEV, entry["path"])
    if not os.path.exists(path):
        changed.append((entry["path"], "MISSING", entry["sha256"], ""))
        continue
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""): h.update(chunk)
    current_sha = h.hexdigest()
    if current_sha != entry["sha256"]:
        changed.append((entry["path"], "MODIFIED", entry["sha256"], current_sha))

if not changed:
    print("CLEAN: dev copy matches Phase 0 baseline exactly.")
else:
    print(f"{len(changed)} changes since Phase 0 baseline:")
    for path, status, orig, current in changed:
        print(f"  {status}  {path}")
```

---

## 3. How to compare against the active deployment snapshot

The active snapshot is always available at:
```
/Workspace/Users/488a0acb-5804-42f0-98b1-a02cc13c4573/src/01f181110277111f8f8d22379e477ecc
```

Replace the SHA-256 comparison target (`SNAP`) with that path and run the comparison
script from the Phase 0 notebook.

If the snapshot path becomes unavailable (container expired), fall back to the backup:
```
/Workspace/Users/lokesh.choraria@te.com/Transparence/baseline_backups/statefix_phase0_20260718T073833Z
```

---

## 4. How to prevent direct edits to production

- All implementation work is done **only** in `transparence_app_statefix/`.
- Production workspace: `/Workspace/Users/lokesh.choraria@te.com/Transparence/transparence_app/`
  is **read-only** during refactoring phases.
- Before every implementation session, confirm the active deployment ID has not changed.
  If it has changed, re-run Steps 1–5 of Phase 0 before proceeding.
- Never use `editAsset` with the production workspace path as the target.
- Never run `apps deploy` from an implementation chat. Deployment is a separate step.

---

## 5. How to recover if Genie Code partially modifies a file

If an implementation step is interrupted mid-edit:

1. **Stop immediately.** Do not attempt to resume.
2. **Do not save conclusions to memory.**
3. Run the manifest comparison (Section 2 above) to identify which files changed.
4. For each changed file, inspect whether it is corrupt:
   ```python
   import ast
   with open(path) as f: ast.parse(f.read())
   ```
5. If the file fails `ast.parse`, restore it from the backup:
   ```python
   shutil.copy2(os.path.join(BACKUP, relative_path), path)
   ```
6. Re-run the targeted tests to confirm the baseline is restored before proceeding.
7. Record the incident in the Phase N log.

---

## Reference Paths

| Resource | Path |
|---|---|
| Development copy | /Workspace/Users/lokesh.choraria@te.com/Transparence/transparence_app_statefix |
| Production workspace | /Workspace/Users/lokesh.choraria@te.com/Transparence/transparence_app |
| Active snapshot | /Workspace/Users/488a0acb-5804-42f0-98b1-a02cc13c4573/src/01f181110277111f8f8d22379e477ecc |
| Phase 0 backup | /Workspace/Users/lokesh.choraria@te.com/Transparence/baseline_backups/statefix_phase0_20260718T073833Z |
| Backup manifest | (backup path)/MANIFEST.json |
| Integrity manifest | (dev copy)/SOURCE_INTEGRITY_BASELINE.json |
