# Release-gate hotfix

This update fixes the final clean-build validation failures:

- missing optional award/draft/captain tables are reported as unavailable by
  `historic_grids.py` instead of being executed as broken squares;
- club captaincy is tested as a supported optional criterion;
- the four intended `meta` rows are checked by key and uniqueness;
- source files are read explicitly as UTF-8 on Windows.

Apply from the project root:

```powershell
python .\apply_release_gate_hotfix.py .
python -m compileall -q .
python .\test_integration.py
python .\test_awards_integration.py --db .\gridley.db
```
