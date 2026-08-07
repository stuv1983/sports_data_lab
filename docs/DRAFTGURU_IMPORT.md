# Draftguru import

The supported pipeline is the cached-export/person-link pipeline:

```powershell
python -m utils.afl.load_draftguru --root data\draftguru
python -m afl.link_draft
python -m afl.link_people
```

`fetch_draft.py` is obsolete and must be deleted. It writes a small legacy
`draft` table without `player_url`, `dg_person_id`, `signing_kind` or the other
columns required by awards and person linking. Running it over a modern
database would replace the working table and break award/signing constraints.

## Expected tables

| Table | Purpose |
|---|---|
| `dg_people` | Stable Draftguru person identity |
| `draft` | Draft, trade and signing records |
| `draft_links` | Auditable draft-row to AFL-player links |
| `awards` | Long-form award results |
| `all_australian` | Team selections and captain flags |
| `person_links` | Stable person to AFL-player links |

Only `unique`, `resolved` and, for person links, `from_draft` statuses are
trusted by the solver. Ambiguous and unmatched records remain available for
audit but cannot create an answer.

## Verification

```sql
SELECT COUNT(*) FROM draft;
SELECT match_status, COUNT(*) FROM draft_links GROUP BY match_status;
SELECT match_status, COUNT(*) FROM person_links GROUP BY match_status;
SELECT award_slug, COUNT(*) FROM awards GROUP BY award_slug ORDER BY 2 DESC;
```

The modern `draft` table should include at least:

```text
player, player_url, name_key, draft_year, draft_type, pick, club,
signing_kind, original_club, dg_person_id
```

For a top-pick sanity check:

```sql
SELECT player, pick, club, draft_type
FROM draft
WHERE draft_year = 2013 AND LOWER(draft_type) LIKE '%national%'
ORDER BY pick LIMIT 5;
```

Pick 1 should be Tom Boyd to GWS.
