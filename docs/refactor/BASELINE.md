# Baseline Test Results

Recorded before any Phase A changes, on branch `restructure`.

## Command
```
.venv\Scripts\python.exe -m pytest -m "not live" --ignore=tests/golden -q --tb=no
```

## Result (2026-10-09)
- **537 passed**
- **17 skipped**
- **8 deselected** (marked `live`)
- **1 xfailed**
- **2 warnings** (non-fatal: Qdrant payload index no-op in local mode; httpx deprecation)
- Exit code: **0**

## Pre-existing failures
None — all non-live tests pass. The xfailed test is an expected failure already marked in the test suite.

## Baseline test counts to maintain after each phase
- passed: 537
- skipped: 17 (may increase as new skips are added legitimately)
- failed: 0
