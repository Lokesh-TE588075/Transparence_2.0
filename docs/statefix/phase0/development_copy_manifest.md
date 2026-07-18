# Development Copy Manifest

## Phase 0 — TransparencE Statefix Refactoring

Recorded: 2026-07-18

---

## Development Copy

| Field | Value |
|---|---|
| Path | /Workspace/Users/lokesh.choraria@te.com/Transparence/transparence_app_statefix |
| Source | Active deployed snapshot |
| Snapshot path | /Workspace/Users/488a0acb-5804-42f0-98b1-a02cc13c4573/src/01f181110277111f8f8d22379e477ecc |
| Active deployment ID | 01f181110277111f8f8d22379e477ecc |
| Files copied | 107 |
| Files excluded | node_modules/, __pycache__/, .pytest_cache/, *.pyc |
| Copy result | PERFECT MATCH — 0 SHA differences |
| Created | 2026-07-18 |

## Baseline Backup

| Field | Value |
|---|---|
| Backup path | /Workspace/Users/lokesh.choraria@te.com/Transparence/baseline_backups/statefix_phase0_20260718T073833Z |
| Manifest file | MANIFEST.json (inside backup path) |
| Files backed up | 107 |
| Total size | 2,069,724 bytes |
| Source | Active deployed snapshot |

## Phase 0 Added Files (not part of production application)

The following files were added to the development copy during Phase 0.
None of them are part of the deployable application.

| File | Purpose |
|---|---|
| .gitignore | Git exclusion rules for future version control |
| SOURCE_INTEGRITY_BASELINE.json | SHA-256 manifest for all files at baseline |
| docs/statefix/phase0/*.md | Phase 0 documentation (this directory) |

## Top-Level Structure

```
transparence_app_statefix/
├── .databricksignore
├── .gitignore                          (Phase 0 added)
├── README.md
├── SOURCE_INTEGRITY_BASELINE.json      (Phase 0 added)
├── app.yaml
├── requirements.txt
├── app/
│   ├── __init__.py
│   ├── main.py
│   ├── business_rules/
│   ├── config/
│   ├── jobs/
│   ├── models/
│   ├── routes/
│   └── services/
├── docs/
│   └── statefix/
│       └── phase0/                     (Phase 0 added)
├── frontend/
│   ├── package.json
│   ├── package-lock.json
│   └── src/
├── static/
│   ├── index.html
│   └── assets/
└── tests/
```
