"""Local accounts, access policies, and saved grids for the Streamlit app.

This database is deliberately separate from the sport databases.  The latter
can be rebuilt from source data; user accounts and saved grids must survive
those rebuilds and must never grant write access to sports statistics.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3


DB_PATH = Path(os.environ.get(
    "SPORTS_DATA_LAB_ACCOUNTS_DB",
    Path(__file__).resolve().parent / "data" / "app" / "accounts.db",
))

FEATURES = {
    "play_grids": ("Play Grids", "public"),
    "grid_solver": ("Grid Solver", "admin"),
    "advanced_search": ("Advanced Search", "member"),
    "game_lab": ("Game Lab", "member"),
    "database_health": ("Database Health", "admin"),
    "database_editing": ("Database editing", "admin"),
}
AUDIENCES = ("public", "member", "selected", "admin")
VERIFICATION_TTL = timedelta(hours=24)


class AccountError(ValueError):
    """A safe validation message that can be shown to a user."""


@dataclass(frozen=True)
class User:
    id: int
    email: str
    display_name: str
    role: str
    active: bool

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


def _connect(path=DB_PATH):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path, timeout=10)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("PRAGMA busy_timeout=10000")
    return con


def ensure_schema(path=DB_PATH):
    with _connect(path) as con:
        con.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY,
                email TEXT NOT NULL UNIQUE COLLATE NOCASE,
                display_name TEXT NOT NULL,
                password_digest TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'member'
                    CHECK (role IN ('member', 'admin')),
                active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
                created_at TEXT NOT NULL,
                last_login_at TEXT,
                email_verified INTEGER NOT NULL DEFAULT 0 CHECK (email_verified IN (0, 1)),
                verification_token TEXT,
                verification_sent_at TEXT
            );
            CREATE TABLE IF NOT EXISTS feature_access (
                feature TEXT PRIMARY KEY,
                audience TEXT NOT NULL
                    CHECK (audience IN ('public', 'member', 'selected', 'admin'))
            );
            CREATE TABLE IF NOT EXISTS feature_grants (
                feature TEXT NOT NULL,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                PRIMARY KEY (feature, user_id)
            );
            CREATE TABLE IF NOT EXISTS saved_grids (
                id INTEGER PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                sport_key TEXT NOT NULL,
                name TEXT NOT NULL,
                definition_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE (user_id, sport_key, name)
            );
            CREATE INDEX IF NOT EXISTS saved_grids_owner
                ON saved_grids(user_id, sport_key, updated_at DESC);
            CREATE TABLE IF NOT EXISTS game_stats (
                id INTEGER PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                game_type TEXT NOT NULL,
                score INTEGER NOT NULL,
                played_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS game_stats_leaderboard
                ON game_stats(game_type, score DESC);
        """)

        user_columns = {
            row[1] for row in con.execute("PRAGMA table_info(users)")
        }
        if "email_verified" not in user_columns:
            con.execute(
                "ALTER TABLE users ADD COLUMN email_verified INTEGER NOT NULL "
                "DEFAULT 0 CHECK (email_verified IN (0, 1))"
            )
        if "verification_token" not in user_columns:
            con.execute("ALTER TABLE users ADD COLUMN verification_token TEXT")
        if "verification_sent_at" not in user_columns:
            con.execute("ALTER TABLE users ADD COLUMN verification_sent_at TEXT")

        # Rows created before email verification existed have no token. Keep
        # those legacy accounts usable while leaving new, token-bearing rows
        # unverified.
        con.execute(
            "UPDATE users SET email_verified=1 "
            "WHERE email_verified=0 AND verification_token IS NULL"
        )

        # The original feature_access CHECK did not include 'public'. SQLite
        # cannot alter CHECK constraints, so rebuild this small policy table.
        feature_sql = con.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type='table' AND name='feature_access'"
        ).fetchone()[0]
        if "'public'" not in feature_sql.casefold():
            con.executescript("""
                ALTER TABLE feature_access RENAME TO feature_access_legacy;
                CREATE TABLE feature_access (
                    feature TEXT PRIMARY KEY,
                    audience TEXT NOT NULL CHECK (
                        audience IN ('public', 'member', 'selected', 'admin')
                    )
                );
                INSERT INTO feature_access(feature, audience)
                    SELECT feature, audience FROM feature_access_legacy;
                DROP TABLE feature_access_legacy;
            """)

        # Hash tokens created by the short-lived plaintext implementation.
        for row in con.execute(
            "SELECT id, verification_token FROM users "
            "WHERE verification_token IS NOT NULL "
            "AND verification_token NOT LIKE 'sha256$%'"
        ):
            con.execute(
                "UPDATE users SET verification_token=?, "
                "verification_sent_at=COALESCE(verification_sent_at, ?) "
                "WHERE id=?",
                (_verification_digest(row[1]), _now(), row[0]),
            )
        con.executemany(
            "INSERT OR IGNORE INTO feature_access(feature, audience) VALUES (?, ?)",
            [(key, default) for key, (_, default) in FEATURES.items()],
        )


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _normalise_email(email):
    value = str(email).strip().casefold()
    if len(value) > 254 or value.count("@") != 1:
        raise AccountError("Enter a valid email address.")
    local, domain = value.rsplit("@", 1)
    labels = domain.split(".")
    local_ok = (
        1 <= len(local) <= 64
        and not local.startswith(".")
        and not local.endswith(".")
        and ".." not in local
        and re.fullmatch(r"[a-z0-9!#$%&'*+/=?^_`{|}~.-]+", local)
    )
    domain_ok = (
        len(labels) >= 2
        and all(
            1 <= len(label) <= 63
            and re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
            for label in labels
        )
        and len(labels[-1]) >= 2
    )
    if not local_ok or not domain_ok:
        raise AccountError("Enter a valid email address.")
    return value


def _password_digest(password, salt=None):
    if salt is None:
        salt = secrets.token_bytes(16)
    derived = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return f"scrypt-v1${salt.hex()}${derived.hex()}"


def _password_matches(password, encoded):
    try:
        version, salt_hex, expected = encoded.split("$", 2)
        if version != "scrypt-v1":
            return False
        actual = _password_digest(password, bytes.fromhex(salt_hex)).split("$", 2)[2]
        return hmac.compare_digest(actual, expected)
    except (TypeError, ValueError):
        return False


def _user(row):
    if row is None:
        return None
    return User(row["id"], row["email"], row["display_name"],
                row["role"], bool(row["active"]))

def _verification_digest(token):
    return "sha256$" + hashlib.sha256(str(token).encode("utf-8")).hexdigest()


def send_validation_email(email, token):
    """Sends verification email using real SMTP if configured, else mock."""
    base_url = os.environ.get(
        "SPORTS_DATA_LAB_BASE_URL", "http://localhost:8501"
    ).rstrip("/")
    link = f"{base_url}/?verify={token}"
    msg_body = f"Click the link to verify your Sports Data Lab account: {link}"
    
    smtp_server = os.environ.get("SMTP_SERVER")
    if smtp_server:
        import smtplib
        from email.mime.text import MIMEText
        
        smtp_port = int(os.environ.get("SMTP_PORT", 587))
        smtp_user = os.environ.get("SMTP_USER")
        smtp_pass = os.environ.get("SMTP_PASSWORD")
        smtp_from = os.environ.get("SMTP_FROM", "noreply@sportsdatalab.com")
        
        msg = MIMEText(msg_body)
        msg["Subject"] = "Verify your Sports Data Lab account"
        msg["From"] = smtp_from
        msg["To"] = email
        
        try:
            with smtplib.SMTP(smtp_server, smtp_port) as server:
                server.starttls()
                if smtp_user and smtp_pass:
                    server.login(smtp_user, smtp_pass)
                server.send_message(msg)
        except Exception as e:
            # If SMTP fails, log it and fallback to mock
            print(f"Failed to send email via SMTP: {e}")
            _mock_email(email, link)
    else:
        _mock_email(email, link)

def _mock_email(email, link):
    log_path = Path(__file__).resolve().parent / "logs" / "emails.txt"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    msg = f"To: {email}\nSubject: Verify your Sports Data Lab account\n\nClick the link to verify: {link}\n\n"
    with log_path.open("a", encoding="utf-8") as f:
        f.write(msg)

def verify_email(token, path=DB_PATH):
    con = _connect(path)
    try:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute(
            "SELECT id, verification_sent_at FROM users "
            "WHERE verification_token=?",
            (_verification_digest(token),),
        ).fetchone()
        if not row:
            con.rollback()
            return False
        try:
            sent_at = datetime.fromisoformat(row["verification_sent_at"])
        except (TypeError, ValueError):
            con.rollback()
            return False
        if sent_at.tzinfo is None:
            sent_at = sent_at.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) - sent_at > VERIFICATION_TTL:
            con.rollback()
            return False
        con.execute(
            "UPDATE users SET email_verified=1, verification_token=NULL, "
            "verification_sent_at=NULL WHERE id=?", (row["id"],)
        )
        con.commit()
        return True
    except Exception:
        con.rollback()
        return False
    finally:
        con.close()


def register(display_name, email, password, path=DB_PATH):
    """Create an account. The very first account safely bootstraps admin."""
    display_name = " ".join(str(display_name).split())
    if not 2 <= len(display_name) <= 80:
        raise AccountError("Display name must be between 2 and 80 characters.")
    email = _normalise_email(email)
    if len(password) < 10:
        raise AccountError("Password must be at least 10 characters.")
    if len(password) > 512:
        raise AccountError("Password is too long.")

    ensure_schema(path)
    con = _connect(path)
    try:
        con.execute("BEGIN IMMEDIATE")
        role = "admin" if con.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0 else "member"
        token = secrets.token_urlsafe(32)
        cur = con.execute(
            "INSERT INTO users(email, display_name, password_digest, role, "
            "created_at, verification_token, verification_sent_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (email, display_name, _password_digest(password), role, _now(),
             _verification_digest(token), _now()),
        )
        send_validation_email(email, token)
        con.commit()
        return get_user(cur.lastrowid, path), role == "admin"
    except sqlite3.IntegrityError as exc:
        con.rollback()
        raise AccountError("An account already exists for that email address.") from exc
    finally:
        con.close()


def authenticate(email, password, path=DB_PATH):
    try:
        email = _normalise_email(email)
    except AccountError:
        return None
    ensure_schema(path)
    with _connect(path) as con:
        row = con.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
        if not row:
            return None
        if "email_verified" in row.keys() and row["email_verified"] == 0:
            raise AccountError("Please check your email to verify your account before logging in.")
        if not _password_matches(password, row["password_digest"]) or not row["active"]:
            return None
        con.execute("UPDATE users SET last_login_at=? WHERE id=?", (_now(), row["id"]))
        return _user(row)


def get_user(user_id, path=DB_PATH):
    if user_id is None:
        return None
    ensure_schema(path)
    with _connect(path) as con:
        return _user(con.execute(
            "SELECT * FROM users WHERE id=? AND active=1", (int(user_id),)).fetchone())


def feature_policies(path=DB_PATH):
    ensure_schema(path)
    with _connect(path) as con:
        return dict(con.execute("SELECT feature, audience FROM feature_access"))


def can_access(user, feature, path=DB_PATH):
    """Re-read roles and grants from SQLite; UI state is not authorization."""
    ensure_schema(path)
    with _connect(path) as con:
        row = con.execute(
            "SELECT audience FROM feature_access WHERE feature=?", (feature,)).fetchone()
        audience = row[0] if row else FEATURES.get(feature, (None, "admin"))[1]

    if audience == "public":
        return True

    if user is None:
        return False
    current = get_user(user.id, path)
    if current is None:
        return False
    if current.is_admin:
        return True

    if audience == "member":
        return True
    if audience == "selected":
        with _connect(path) as con:
            return con.execute(
                "SELECT 1 FROM feature_grants WHERE feature=? AND user_id=?",
                (feature, current.id)).fetchone() is not None
    return False


def list_users(path=DB_PATH):
    ensure_schema(path)
    with _connect(path) as con:
        return [_user(row) for row in con.execute(
            "SELECT * FROM users ORDER BY display_name COLLATE NOCASE, email")]


def _require_admin(actor_id, con):
    row = con.execute(
        "SELECT role, active FROM users WHERE id=?", (int(actor_id),)).fetchone()
    if row is None or row["role"] != "admin" or not row["active"]:
        raise PermissionError("Administrator access required.")


def set_user_access(actor_id, user_id, *, role=None, active=None, path=DB_PATH):
    ensure_schema(path)
    with _connect(path) as con:
        _require_admin(actor_id, con)
        target = con.execute("SELECT role, active FROM users WHERE id=?", (user_id,)).fetchone()
        if target is None:
            raise AccountError("User not found.")
        new_role = role if role is not None else target["role"]
        new_active = int(active if active is not None else target["active"])
        if new_role not in ("member", "admin"):
            raise AccountError("Unknown role.")
        if target["role"] == "admin" and target["active"] and (new_role != "admin" or not new_active):
            admins = con.execute(
                "SELECT COUNT(*) FROM users WHERE role='admin' AND active=1").fetchone()[0]
            if admins <= 1:
                raise AccountError("At least one active administrator is required.")
        con.execute("UPDATE users SET role=?, active=? WHERE id=?",
                    (new_role, new_active, user_id))


def set_feature_policy(actor_id, feature, audience, path=DB_PATH):
    if feature not in FEATURES or audience not in AUDIENCES:
        raise AccountError("Unknown feature access setting.")
    ensure_schema(path)
    with _connect(path) as con:
        _require_admin(actor_id, con)
        con.execute(
            "INSERT INTO feature_access(feature, audience) VALUES (?, ?) "
            "ON CONFLICT(feature) DO UPDATE SET audience=excluded.audience",
            (feature, audience),
        )


def feature_grants(feature, path=DB_PATH):
    ensure_schema(path)
    with _connect(path) as con:
        return {row[0] for row in con.execute(
            "SELECT user_id FROM feature_grants WHERE feature=?", (feature,))}


def set_feature_grant(actor_id, feature, user_id, granted, path=DB_PATH):
    if feature not in FEATURES:
        raise AccountError("Unknown feature.")
    ensure_schema(path)
    with _connect(path) as con:
        _require_admin(actor_id, con)
        if granted:
            con.execute("INSERT OR IGNORE INTO feature_grants VALUES (?, ?)",
                        (feature, user_id))
        else:
            con.execute("DELETE FROM feature_grants WHERE feature=? AND user_id=?",
                        (feature, user_id))


def _axis_payload(axes):
    if len(axes) != 3:
        raise AccountError("A grid must have exactly three rows and three columns.")
    payload = []
    for label, constraint in axes:
        if constraint is None:
            raise AccountError("Define all six axes before saving the grid.")
        sql, params = constraint
        if not isinstance(sql, str) or len(sql) > 20_000 or ";" in sql:
            raise AccountError("This grid contains an invalid criterion.")
        payload.append({"label": str(label)[:300], "sql": sql, "params": list(params)})
    return payload


def save_grid(user_id, sport_key, name, rows, cols, path=DB_PATH):
    owner = get_user(user_id, path)
    if owner is None:
        raise PermissionError("Sign in to save grids.")
    name = " ".join(str(name).split())
    if not 1 <= len(name) <= 80:
        raise AccountError("Grid name must be between 1 and 80 characters.")
    definition = json.dumps({"rows": _axis_payload(rows), "cols": _axis_payload(cols)},
                            separators=(",", ":"))
    now = _now()
    with _connect(path) as con:
        con.execute("""
            INSERT INTO saved_grids(user_id, sport_key, name, definition_json,
                                    created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id, sport_key, name) DO UPDATE SET
                definition_json=excluded.definition_json,
                updated_at=excluded.updated_at
        """, (owner.id, str(sport_key), name, definition, now, now))


def list_saved_grids(user_id, sport_key, path=DB_PATH):
    if get_user(user_id, path) is None:
        return []
    with _connect(path) as con:
        return [dict(row) for row in con.execute(
            "SELECT id, name, created_at, updated_at FROM saved_grids "
            "WHERE user_id=? AND sport_key=? ORDER BY updated_at DESC, name",
            (user_id, sport_key))]


def load_grid(user_id, grid_id, path=DB_PATH):
    if get_user(user_id, path) is None:
        raise PermissionError("Sign in to open grids.")
    with _connect(path) as con:
        row = con.execute(
            "SELECT definition_json FROM saved_grids WHERE id=? AND user_id=?",
            (grid_id, user_id)).fetchone()
    if row is None:
        raise AccountError("Saved grid not found.")
    data = json.loads(row[0])
    return {
        side: [(axis["label"], (axis["sql"], axis["params"]))
               for axis in data[side]]
        for side in ("rows", "cols")
    }

def list_all_grids(sport_key, path=DB_PATH):
    with _connect(path) as con:
        return [dict(row) for row in con.execute(
            "SELECT id, name, created_at, updated_at FROM saved_grids "
            "WHERE sport_key=? ORDER BY updated_at DESC, name",
            (sport_key,))]

def load_any_grid(grid_id, path=DB_PATH):
    with _connect(path) as con:
        row = con.execute(
            "SELECT definition_json FROM saved_grids WHERE id=?",
            (grid_id,)).fetchone()
    if row is None:
        raise AccountError("Saved grid not found.")
    data = json.loads(row[0])
    return {
        side: [(axis["label"], (axis["sql"], axis["params"]))
               for axis in data[side]]
        for side in ("rows", "cols")
    }


def delete_grid(user_id, grid_id, path=DB_PATH):
    if get_user(user_id, path) is None:
        raise PermissionError("Sign in to delete grids.")
    with _connect(path) as con:
        con.execute("DELETE FROM saved_grids WHERE id=? AND user_id=?",
                    (grid_id, user_id))

def log_game_stat(user_id, game_type, score, path=DB_PATH):
    ensure_schema(path)
    with _connect(path) as con:
        con.execute(
            "INSERT INTO game_stats(user_id, game_type, score, played_at) VALUES (?, ?, ?, ?)",
            (user_id, game_type, score, _now())
        )

def get_user_stats(user_id, path=DB_PATH):
    ensure_schema(path)
    with _connect(path) as con:
        cur = con.execute(
            "SELECT game_type, COUNT(*) as games_played, MAX(score) as top_score, AVG(score) as avg_score "
            "FROM game_stats WHERE user_id=? GROUP BY game_type",
            (user_id,)
        )
        stats = {}
        for row in cur:
            stats[row["game_type"]] = {
                "games_played": row["games_played"],
                "top_score": row["top_score"],
                "avg_score": round(row["avg_score"], 1)
            }
        return stats

def get_leaderboard(game_type, path=DB_PATH, limit=50):
    """One row per player: their best score, and when they first reached it.

    Grouping matters. Ranking raw game_stats rows let a single player who
    plays often occupy every place on a board headed "Top Score".
    """
    ensure_schema(path)
    with _connect(path) as con:
        cur = con.execute(
            """
            SELECT u.display_name, s.score, s.played_at
            FROM game_stats s
            JOIN users u ON s.user_id = u.id
            WHERE s.game_type = ?
              AND s.id = (
                  SELECT b.id FROM game_stats b
                  WHERE b.user_id = s.user_id AND b.game_type = s.game_type
                  ORDER BY b.score DESC, b.played_at ASC, b.id ASC
                  LIMIT 1)
            ORDER BY s.score DESC, s.played_at ASC
            LIMIT ?
            """,
            (game_type, limit)
        )
        return [dict(r) for r in cur]
