import datetime as dt

import database_updates as updates


def test_2026_afl_annual_dates_match_announced_calendar():
    assert updates.last_saturday_in_september(2026) == dt.date(2026, 9, 26)
    assert updates.brownlow_refresh_date(2026) == dt.date(2026, 9, 22)
    assert updates.grand_final_refresh_date(2026) == dt.date(2026, 9, 27)


def test_every_regular_sport_has_a_build_and_strict_health_check():
    planned = updates.plan("regular", updates.SPORT_KEYS)
    for sport in updates.SPORT_KEYS:
        labels = [step.label for key, step in planned if key == sport]
        assert any("rebuild" in label.lower() for label in labels)
        assert labels[-1] == "Strict database health check"


def test_annual_due_guards_only_match_the_intended_day():
    assert updates.event_is_due("brownlow-awards", dt.date(2026, 9, 22))
    assert not updates.event_is_due("brownlow-awards", dt.date(2026, 9, 29))
    assert updates.event_is_due("grand-final-awards", dt.date(2026, 9, 27))


def test_grand_final_job_rebuilds_scores_before_awards():
    planned = updates.plan("grand-final-awards", ["afl"])
    labels = [step.label for _, step in planned]
    assert labels[0] == "Fetch and rebuild AFL"
    assert "Load Brownlow CSVs" in labels
