"""db_pool.py -- One read-only SQLite connection per thread, per database.

WHY THIS IS NOT A CACHED SINGLETON
----------------------------------
The Streamlit app used to hold a single ``sqlite3.Connection`` in
``@st.cache_resource`` and hand it to every script-runner thread. Read-only
access felt safe enough to share. It is not.

CPython's ``sqlite3`` keeps a prepared-statement cache on the connection
object and does not lock it, and ``check_same_thread=False`` switches off
the check that would otherwise catch the sharing. Two overlapping reruns --
a second browser tab, or a widget touched while a square is still solving
-- then step on each other's statements. That surfaces two ways:

    sqlite3.InterfaceError: bad parameter or other API misuse

and, with no error raised at all, a query returning zero rows against data
that plainly matches. The silent one is the dangerous one: an empty grid
square is indistinguishable from a genuinely hard question, so the board
reports "no answer" and is simply believed.

A read-only connection per thread costs a file descriptor. The OS page
cache and the mmap are shared, so the data is not duplicated in memory.
"""

import sqlite3
import threading

#: Read-only tuning applied to every handle. Sizes are deliberate: the
#: cache is 64 MB and the mmap 256 MB, which covers the whole AFL database.
PRAGMAS = (
    "query_only = ON",
    "temp_store = MEMORY",
    "cache_size = -65536",
    "mmap_size = 268435456",
    "busy_timeout = 5000",
)

_LOCAL = threading.local()


def open_read_only(db, pragmas=PRAGMAS, timeout=5.0):
    """A tuned read-only handle on one database file."""
    con = sqlite3.connect(
        f"file:{db}?mode=ro", uri=True, check_same_thread=False,
        timeout=timeout,
    )
    for pragma in pragmas:
        con.execute(f"PRAGMA {pragma}")
    return con


def get_con(db, revision):
    """The calling thread's connection to one revision of one database.

    `revision` is any value that changes when the file is replaced -- the
    app passes (mtime_ns, size). A new revision closes this thread's handle
    on the old file rather than accumulating one per rebuild.
    """
    cache = getattr(_LOCAL, "by_revision", None)
    if cache is None:
        cache = _LOCAL.by_revision = {}

    key = (db, revision)
    con = cache.get(key)
    if con is None:
        for stale in [k for k in cache if k[0] == db]:
            cache.pop(stale).close()
        con = cache[key] = open_read_only(db)
    return con


def close_all():
    """Close every connection this thread holds. For tests and teardown."""
    cache = getattr(_LOCAL, "by_revision", None)
    if not cache:
        return
    for key in list(cache):
        cache.pop(key).close()
