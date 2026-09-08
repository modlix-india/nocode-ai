"""The migration runner's statement splitter.

Regression cover for a bug that made a whole migration silently never apply:
`sql.split(";")` cut through a quoted column COMMENT, so V13 (lore) failed on
its second statement, was never recorded, and lore's tables were never created
by the runner. It only worked locally because the DDL had been run by hand.
"""

from pathlib import Path

import pytest

from app.db.migrations import MIGRATIONS_DIR, _split_statements, get_migration_files


def test_splits_on_plain_semicolons():
    assert _split_statements("SELECT 1; SELECT 2") == ["SELECT 1", "SELECT 2"]


def test_ignores_trailing_semicolon_and_blank_statements():
    assert _split_statements("SELECT 1;;\n  ;") == ["SELECT 1"]


def test_semicolon_inside_a_single_quoted_comment_does_not_split():
    sql = (
        "CREATE TABLE t (\n"
        "  c TINYINT COMMENT '0-100 at LAST_CONFIRMED_AT; decayed at read time'\n"
        ");"
    )
    statements = _split_statements(sql)
    assert len(statements) == 1
    assert statements[0].endswith(")")
    assert "decayed at read time" in statements[0]


@pytest.mark.parametrize("quote", ["'", '"', "`"])
def test_every_mysql_quote_character_protects_a_semicolon(quote):
    sql = f"SELECT {quote}a;b{quote} AS x; SELECT 2"
    assert _split_statements(sql) == [f"SELECT {quote}a;b{quote} AS x", "SELECT 2"]


def test_backslash_escaped_quote_does_not_end_the_quoted_run():
    # The \' is literal, so the run stays open across the semicolon.
    sql = r"SELECT 'it\'s; fine' AS x; SELECT 2"
    assert _split_statements(sql) == [r"SELECT 'it\'s; fine' AS x", "SELECT 2"]


def test_doubled_quote_does_not_end_the_quoted_run():
    sql = "SELECT 'it''s; fine' AS x; SELECT 2"
    assert _split_statements(sql) == ["SELECT 'it''s; fine' AS x", "SELECT 2"]


def test_adjacent_quoted_literals_are_tracked_independently():
    sql = "INSERT INTO t VALUES ('a;1', 'b;2'); SELECT 2"
    assert _split_statements(sql) == ["INSERT INTO t VALUES ('a;1', 'b;2')", "SELECT 2"]


def test_lore_migration_is_six_statements_not_eight():
    """V13 is what caught this: two COMMENTs contain a semicolon."""
    path = next(MIGRATIONS_DIR.glob("V13__*.sql"), None)
    if path is None:
        pytest.skip("V13 migration not present")
    body = "\n".join(
        line for line in path.read_text(encoding="utf-8").split("\n")
        if not line.strip().startswith("--")
    )
    statements = _split_statements(body)
    assert len(statements) == 6
    assert all(s.upper().startswith("CREATE TABLE") for s in statements)


def test_no_shipped_migration_splits_into_a_fragment():
    """Every statement in every migration must start with a SQL verb.

    A fragment left behind by a bad split starts mid-clause, which is exactly
    how the V13 failure presented.
    """
    verbs = (
        "CREATE", "ALTER", "DROP", "INSERT", "UPDATE", "DELETE",
        "SET", "USE", "RENAME", "TRUNCATE", "REPLACE", "GRANT",
    )
    for version, _description, path in get_migration_files():
        body = "\n".join(
            line for line in path.read_text(encoding="utf-8").split("\n")
            if not line.strip().startswith("--")
        )
        for stmt in _split_statements(body):
            assert stmt.upper().startswith(verbs), (
                f"{version} ({Path(path).name}) produced a fragment: {stmt[:80]!r}"
            )
