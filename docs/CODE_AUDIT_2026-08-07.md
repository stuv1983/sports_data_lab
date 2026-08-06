# Code audit — 7 August 2026

## Scope

Reviewed the 169 Python files in the application, sport adapters, import and
repair utilities, and test suite. The audit covered runtime tests, targeted
static analysis, Streamlit page rendering, query validation, and manual review
of the affected paths.

Database files and source datasets were excluded from edits. Application
smoke tests used the project's read-only SQLite connection pool; tests that
build databases used in-memory connections or temporary directories. The
existing database files retained their original modification times.

## Issues fixed

### MLB WAR grid criteria could not be configured correctly

The two MLB builders `X+ WAR in a season` and `X+ career WAR` declare a `war`
argument, but `app.axis_widget` had no handler for it. The generic fallback
rendered an integer input and coerced defaults with `int()`, so decimal WAR
thresholds such as 4.5 were unavailable. The board header also showed the
generic builder name instead of the selected threshold.

Changes:

- Added a decimal WAR input with 0.5 increments.
- Added appropriate defaults: 5.0 for a season and 50.0 for a career.
- Added board labels containing the selected threshold.
- Extended the builder/UI contract regression check to cover `war`.

### Advanced Search accepted invalid numeric input

`limit:1.5` was silently truncated to `limit:1`. Non-finite values such as
`nan` and `inf` also passed numeric parsing and could reach SQLite comparisons.

Changes:

- Require `limit` to be a whole number between 1 and 500.
- Reject non-finite numeric values with `QuerySyntaxError`.
- Added regression cases for fractional limits, `nan`, and `inf`.

### Widget formatter closures captured changing loop variables

Four `format_func` lambdas in the axis editor closed over the loop-local
option list. Streamlit currently evaluates these during widget construction,
but retaining a formatter could make it observe a later iteration's list.
Each formatter now binds its own list explicitly.

## Minor hygiene changes

- Renamed a local `field` loop variable that shadowed `dataclasses.field`.
- Removed unnecessary `f` prefixes from static HTML strings.
- Restored a final newline in `app.py`.

## Validation

- Initial full suite: 574 passed, 12 skipped, 1 failed. The sole failure was
  the missing MLB WAR UI handler described above.
- Focused regression suites after the fixes: 27 passed.
- Final full suite after all changes: 578 passed, 12 skipped.
- Streamlit smoke matrix: all 11 navigation pages rendered for AFL, MLB, NBA,
  and NFL (44 combinations) with zero exceptions and zero `st.error` results.
- Ruff correctness checks (`E9`, `F63`, `F7`, `F82`): passed.
- Ruff checks on changed files for parse/name errors and unsafe loop closures:
  passed.

The 12 skipped tests are the repository's explicitly opt-in live/clean-build
checks. They require external source data or the `SDL_INTEGRATION=1` release
gate and were not enabled because this audit was required not to edit
databases.
