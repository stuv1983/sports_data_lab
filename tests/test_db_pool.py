#!/usr/bin/env python3
"""A SQLite connection is never shared between threads.

The app held one read-only connection in @st.cache_resource and handed it
to every Streamlit script-runner thread. Read-only felt safe. It is not:
CPython's sqlite3 keeps an unlocked prepared-statement cache on the
connection, so two overlapping reruns corrupt each other's statements.

Under eight concurrent threads the shared connection produced hundreds of
`InterfaceError: bad parameter or other API misuse` -- and, with no error
raised at all, queries returning zero rows against data that matches. The
silent failure is the one that mattered: an empty grid square looks exactly
like a hard question, so a wrong board is simply believed.

These tests pin the property, not the implementation: whatever get_con
does, concurrent callers must never see an error or a wrong answer.
"""

# --- test bootstrap: run from the repository root, import project modules ---
import os as _os
import sys as _sys

_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if _ROOT not in _sys.path:
    _sys.path.insert(0, _ROOT)
_os.chdir(_ROOT)
# --- end test bootstrap ---


import sqlite3
import threading

import pytest

import db_pool

THREADS = 8
ITERATIONS = 250


@pytest.fixture
def db(tmp_path):
    """A small on-disk database with a known, stable answer."""
    path = tmp_path / "pool.db"
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE games (player_id INTEGER, club TEXT, career_game_no INT);
    """)
    con.executemany(
        "INSERT INTO games VALUES (?,?,?)",
        [(i, "Carlton" if i % 2 else "Geelong", 1) for i in range(400)])
    con.commit()
    con.close()
    return str(path)


def hammer(body):
    """Run `body` on every thread; return (errors, wrong_answers)."""
    errors, wrong = [], []
    barrier = threading.Barrier(THREADS)

    def work():
        barrier.wait()          # maximise overlap
        for _ in range(ITERATIONS):
            try:
                body(wrong)
            except Exception as e:            # noqa: BLE001 - that is the point
                errors.append(f"{type(e).__name__}: {e}")

    threads = [threading.Thread(target=work) for _ in range(THREADS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return errors, wrong


# ------------------------------------------------- the property under test

def test_concurrent_readers_get_no_errors_and_no_wrong_answers(db):
    def body(wrong):
        con = db_pool.get_con(db, "rev-1")
        # A parameterised query on each of two different statements, which
        # is what made the shared connection thrash its statement cache.
        con.execute("SELECT 1 FROM main.sqlite_master "
                    "WHERE type='table' AND name=?", ("games",)).fetchone()
        n = con.execute(
            "SELECT COUNT(*) FROM games WHERE career_game_no=1 AND club IN (?)",
            ("Carlton",)).fetchone()[0]
        if n != 200:
            wrong.append(n)

    errors, wrong = hammer(body)
    db_pool.close_all()
    assert errors == []
    assert wrong == [], f"{len(wrong)} queries returned the wrong count"


def test_each_thread_gets_its_own_connection(db):
    seen, lock = set(), threading.Lock()

    def work():
        con = db_pool.get_con(db, "rev-1")
        with lock:
            seen.add(id(con))

    threads = [threading.Thread(target=work) for _ in range(THREADS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(seen) == THREADS


def test_same_thread_reuses_one_connection(db):
    first = db_pool.get_con(db, "rev-1")
    assert db_pool.get_con(db, "rev-1") is first
    db_pool.close_all()


def test_a_new_revision_replaces_the_old_handle(db):
    first = db_pool.get_con(db, "rev-1")
    second = db_pool.get_con(db, "rev-2")
    assert second is not first
    # The superseded handle is closed, not leaked, so a rebuilt database
    # does not cost a file descriptor per rebuild per thread.
    with pytest.raises(sqlite3.ProgrammingError):
        first.execute("SELECT 1")
    db_pool.close_all()


def test_connections_are_read_only(db):
    con = db_pool.get_con(db, "rev-1")
    with pytest.raises(sqlite3.OperationalError):
        con.execute("INSERT INTO games VALUES (1,'Carlton',1)")
    db_pool.close_all()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
