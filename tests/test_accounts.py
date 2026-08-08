import sqlite3

import pytest

import accounts


@pytest.fixture(autouse=True)
def sent_emails(monkeypatch):
    sent = []
    monkeypatch.setattr(
        accounts, "send_validation_email",
        lambda email, token: sent.append((email, token)),
    )
    return sent


def _accounts(tmp_path):
    return tmp_path / "accounts.db"


def test_first_account_bootstraps_admin_and_passwords_are_hashed(
        tmp_path, sent_emails):
    path = _accounts(tmp_path)
    user, first = accounts.register(
        "Site Owner", "Owner@Example.com", "very-long-password", path)
    
    with sqlite3.connect(path) as con:
        con.execute("UPDATE users SET email_verified=1 WHERE id=?", (user.id,))
        con.commit()

    assert first is True
    assert user.role == "admin"
    assert accounts.authenticate(
        "owner@example.com", "very-long-password", path) == user
    assert accounts.authenticate("owner@example.com", "wrong-password", path) is None

    with sqlite3.connect(path) as con:
        stored, verification_token = con.execute(
            "SELECT password_digest, verification_token FROM users"
        ).fetchone()
    assert "very-long-password" not in stored
    assert stored.startswith("scrypt-v1$")
    assert sent_emails[0][1] not in verification_token
    assert verification_token.startswith("sha256$")


def test_email_verification_is_required_and_token_is_single_use(
        tmp_path, sent_emails):
    path = _accounts(tmp_path)
    user, _ = accounts.register(
        "Site Owner", "owner@example.com", "very-long-password", path)

    with pytest.raises(accounts.AccountError, match="verify"):
        accounts.authenticate("owner@example.com", "very-long-password", path)

    token = sent_emails[0][1]
    assert accounts.verify_email(token, path)
    assert accounts.authenticate(
        "owner@example.com", "very-long-password", path) == user
    assert not accounts.verify_email(token, path)


def test_feature_can_be_member_admin_or_selected_user_only(tmp_path):
    path = _accounts(tmp_path)
    admin, _ = accounts.register("Admin User", "admin@example.com", "password-123", path)
    member, _ = accounts.register("Normal User", "member@example.com", "password-123", path)
    
    with sqlite3.connect(path) as con:
        con.execute("UPDATE users SET email_verified=1")
        con.commit()

    assert not accounts.can_access(member, "grid_solver", path)
    accounts.set_feature_policy(admin.id, "grid_solver", "member", path)
    assert accounts.can_access(member, "grid_solver", path)
    accounts.set_feature_policy(admin.id, "grid_solver", "admin", path)
    assert not accounts.can_access(member, "grid_solver", path)
    assert accounts.can_access(admin, "grid_solver", path)

    accounts.set_feature_policy(admin.id, "grid_solver", "selected", path)
    assert not accounts.can_access(member, "grid_solver", path)
    accounts.set_feature_grant(admin.id, "grid_solver", member.id, True, path)
    assert accounts.can_access(member, "grid_solver", path)

    assert accounts.can_access(None, "play_grids", path)
    accounts.set_feature_policy(admin.id, "play_grids", "member", path)
    assert not accounts.can_access(None, "play_grids", path)
    assert accounts.can_access(member, "play_grids", path)
    accounts.set_feature_policy(admin.id, "play_grids", "public", path)
    assert accounts.can_access(None, "play_grids", path)


def test_non_admin_cannot_change_access_and_last_admin_is_protected(tmp_path):
    path = _accounts(tmp_path)
    admin, _ = accounts.register("Admin User", "admin@example.com", "password-123", path)
    member, _ = accounts.register("Normal User", "member@example.com", "password-123", path)
    
    with sqlite3.connect(path) as con:
        con.execute("UPDATE users SET email_verified=1")
        con.commit()

    with pytest.raises(PermissionError):
        accounts.set_feature_policy(member.id, "grid_solver", "admin", path)
    with pytest.raises(accounts.AccountError, match="administrator"):
        accounts.set_user_access(admin.id, admin.id, role="member", path=path)


def test_saved_grids_are_private_and_round_trip_constraints(tmp_path):
    path = _accounts(tmp_path)
    owner, _ = accounts.register("Grid Owner", "owner@example.com", "password-123", path)
    other, _ = accounts.register("Other User", "other@example.com", "password-123", path)
    
    with sqlite3.connect(path) as con:
        con.execute("UPDATE users SET email_verified=1")
        con.commit()
    rows = [(f"row {i}", ("SELECT player_id FROM games WHERE season >= ?", [2000 + i]))
            for i in range(3)]
    cols = [(f"club {i}", ("SELECT player_id FROM games WHERE club_now = ?", [f"C{i}"]))
            for i in range(3)]

    accounts.save_grid(owner.id, "afl", "Friday", rows, cols, path)
    saved = accounts.list_saved_grids(owner.id, "afl", path)
    assert [item["name"] for item in saved] == ["Friday"]
    assert accounts.load_grid(owner.id, saved[0]["id"], path) == {
        "rows": rows, "cols": cols}
    with pytest.raises(accounts.AccountError, match="not found"):
        accounts.load_grid(other.id, saved[0]["id"], path)

    accounts.save_grid(owner.id, "afl", "Friday", rows[::-1], cols, path)
    assert len(accounts.list_saved_grids(owner.id, "afl", path)) == 1
    assert accounts.load_grid(owner.id, saved[0]["id"], path)["rows"] == rows[::-1]


def test_legacy_accounts_are_verified_and_policy_schema_is_migrated(tmp_path):
    path = _accounts(tmp_path)
    with sqlite3.connect(path) as con:
        con.executescript("""
            CREATE TABLE users (
                id INTEGER PRIMARY KEY,
                email TEXT NOT NULL UNIQUE COLLATE NOCASE,
                display_name TEXT NOT NULL,
                password_digest TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'member',
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                last_login_at TEXT
            );
            CREATE TABLE feature_access (
                feature TEXT PRIMARY KEY,
                audience TEXT NOT NULL CHECK (
                    audience IN ('member', 'selected', 'admin')
                )
            );
        """)
        con.execute(
            "INSERT INTO users(email, display_name, password_digest, role, "
            "created_at) VALUES (?, ?, ?, ?, ?)",
            ("legacy@example.com", "Legacy Admin",
             accounts._password_digest("legacy-password"), "admin",
             "2026-01-01T00:00:00+00:00"),
        )

    accounts.ensure_schema(path)
    legacy = accounts.authenticate(
        "legacy@example.com", "legacy-password", path)
    assert legacy is not None
    accounts.set_feature_policy(legacy.id, "play_grids", "public", path)
    assert accounts.can_access(None, "play_grids", path)


@pytest.mark.parametrize("email", [
    "person@example..com",
    ".person@example.com",
    "person@example.com.",
    "person@-example.com",
])
def test_invalid_email_shapes_are_rejected(tmp_path, email):
    with pytest.raises(accounts.AccountError, match="valid email"):
        accounts.register("Test Person", email, "long-enough-password", _accounts(tmp_path))


# ----------------------------------------------------------- game stats

def test_the_leaderboard_ranks_players_not_games(tmp_path):
    """One row per player, their best score.

    Ranking raw game_stats rows let one player who plays often occupy every
    place on a board headed "Top Score".
    """
    path = _accounts(tmp_path)
    keen, _ = accounts.register(
        "Keen Player", "keen@example.com", "long-enough-password", path)
    rare, _ = accounts.register(
        "Rare Player", "rare@example.com", "long-enough-password", path)

    for score in (3, 9, 5, 9):
        accounts.log_game_stat(keen.id, "gridley", score, path)
    accounts.log_game_stat(rare.id, "gridley", 7, path)

    board = accounts.get_leaderboard("gridley", path)
    assert [(row["display_name"], row["score"]) for row in board] == [
        ("Keen Player", 9), ("Rare Player", 7)]
    assert all(row["played_at"] for row in board)


def test_a_board_nobody_has_played_is_empty_not_an_error(tmp_path):
    assert accounts.get_leaderboard("higher_lower", _accounts(tmp_path)) == []


def test_personal_stats_are_grouped_by_game_type(tmp_path):
    path = _accounts(tmp_path)
    user, _ = accounts.register(
        "Stat Watcher", "stats@example.com", "long-enough-password", path)
    for score in (4, 8):
        accounts.log_game_stat(user.id, "gridley", score, path)
    accounts.log_game_stat(user.id, "higher_lower", 12, path)

    stats = accounts.get_user_stats(user.id, path)
    assert stats["gridley"] == {
        "games_played": 2, "top_score": 8, "avg_score": 6.0}
    assert stats["higher_lower"]["top_score"] == 12
