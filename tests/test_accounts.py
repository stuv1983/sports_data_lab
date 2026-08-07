import sqlite3

import pytest

import accounts


def _accounts(tmp_path):
    return tmp_path / "accounts.db"


def test_first_account_bootstraps_admin_and_passwords_are_hashed(tmp_path):
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
        stored = con.execute("SELECT password_digest FROM users").fetchone()[0]
    assert "very-long-password" not in stored
    assert stored.startswith("scrypt-v1$")


def test_feature_can_be_member_admin_or_selected_user_only(tmp_path):
    path = _accounts(tmp_path)
    admin, _ = accounts.register("Admin User", "admin@example.com", "password-123", path)
    member, _ = accounts.register("Normal User", "member@example.com", "password-123", path)
    
    with sqlite3.connect(path) as con:
        con.execute("UPDATE users SET email_verified=1")
        con.commit()

    assert accounts.can_access(member, "grid_solver", path)
    accounts.set_feature_policy(admin.id, "grid_solver", "admin", path)
    assert not accounts.can_access(member, "grid_solver", path)
    assert accounts.can_access(admin, "grid_solver", path)

    accounts.set_feature_policy(admin.id, "grid_solver", "selected", path)
    assert not accounts.can_access(member, "grid_solver", path)
    accounts.set_feature_grant(admin.id, "grid_solver", member.id, True, path)
    assert accounts.can_access(member, "grid_solver", path)


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
