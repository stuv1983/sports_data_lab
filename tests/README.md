# Tests

Run from the repository root:

    python -m pytest tests                  # everything pytest can collect
    python tests/test_core_regressions.py   # or any file directly

Each file adds the repository root to `sys.path` and changes the working
directory to it, so relative paths such as `sql/`, `gridley.db` and
`tests/fixtures/draftguru` resolve the same way in both styles of run.

`fixtures/draftguru` is the tiny hand-made Draftguru tree used by
`test_draftguru.py`. It is source-controlled; the real scraped tree lives in
`data/afl/raw/draftguru` and is ignored by git.
