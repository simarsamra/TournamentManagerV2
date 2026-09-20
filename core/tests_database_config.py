"""Database backend selection in settings.py.

SQLite stays the default so an existing checkout keeps working, PostgreSQL is
opt-in, and a DATABASE_URL overrides both. Getting this wrong points a
deployment at the wrong database, so the resolution is pinned here rather than
trusted.
"""
import pathlib
import sys
from unittest import mock

from django.core.exceptions import ImproperlyConfigured
from django.test import TestCase


SETTINGS = pathlib.Path(__file__).resolve().parent.parent / "tournament_manager" / "settings.py"


def resolve(**env):
    """Execute settings.py in a fresh namespace under the given environment."""
    base = {
        "DJANGO_SECRET_KEY": "test-key-for-database-resolution",
        "DJANGO_DEBUG": "True",
    }
    base.update(env)
    namespace = {"__file__": str(SETTINGS), "__name__": "settings_under_test"}
    argv = sys.argv
    try:
        sys.argv = ["manage.py", "check"]
        with mock.patch.dict("os.environ", base, clear=True):
            exec(compile(SETTINGS.read_text(), str(SETTINGS), "exec"), namespace)
    finally:
        sys.argv = argv
    return namespace["DATABASES"]["default"]


class DefaultBackendTests(TestCase):
    def test_no_configuration_means_sqlite(self):
        db = resolve()
        self.assertEqual(db["ENGINE"], "django.db.backends.sqlite3")
        self.assertTrue(str(db["NAME"]).endswith("db.sqlite3"))

    def test_sqlite_name_is_overridable(self):
        db = resolve(DJANGO_DB_ENGINE="sqlite3", DJANGO_DB_NAME="/tmp/other.sqlite3")
        self.assertEqual(db["NAME"], "/tmp/other.sqlite3")

    def test_an_unknown_engine_is_refused(self):
        with self.assertRaises(ImproperlyConfigured):
            resolve(DJANGO_DB_ENGINE="mysql")


class PostgresBackendTests(TestCase):
    def test_engine_switch_selects_postgres(self):
        db = resolve(
            DJANGO_DB_ENGINE="postgresql", DJANGO_DB_NAME="tm",
            DJANGO_DB_USER="u", DJANGO_DB_PASSWORD="p",
            DJANGO_DB_HOST="h", DJANGO_DB_PORT="6000",
        )
        self.assertEqual(db["ENGINE"], "django.db.backends.postgresql")
        self.assertEqual(
            [db["NAME"], db["USER"], db["PASSWORD"], db["HOST"], db["PORT"]],
            ["tm", "u", "p", "h", "6000"],
        )

    def test_postgres_aliases_are_accepted(self):
        for alias in ("postgresql", "postgres", "psql", "POSTGRES"):
            with self.subTest(alias=alias):
                self.assertEqual(
                    resolve(DJANGO_DB_ENGINE=alias)["ENGINE"],
                    "django.db.backends.postgresql",
                )

    def test_connections_are_reused_by_default(self):
        self.assertEqual(resolve(DJANGO_DB_ENGINE="postgresql")["CONN_MAX_AGE"], 60)

    def test_conn_max_age_is_configurable(self):
        db = resolve(DJANGO_DB_ENGINE="postgresql", DJANGO_CONN_MAX_AGE="0")
        self.assertEqual(db["CONN_MAX_AGE"], 0)


class DatabaseUrlTests(TestCase):
    def test_url_is_parsed(self):
        db = resolve(DATABASE_URL="postgres://user:secret@db.example.com:6543/mydb")
        self.assertEqual(db["ENGINE"], "django.db.backends.postgresql")
        self.assertEqual(
            [db["NAME"], db["USER"], db["PASSWORD"], db["HOST"], db["PORT"]],
            ["mydb", "user", "secret", "db.example.com", "6543"],
        )

    def test_percent_encoded_credentials_are_decoded(self):
        """A password with @ or / in it must survive the URL round trip."""
        db = resolve(DATABASE_URL="postgres://u%40corp:p%40ss%2Fword@h:5432/n")
        self.assertEqual(db["USER"], "u@corp")
        self.assertEqual(db["PASSWORD"], "p@ss/word")

    def test_url_wins_over_the_engine_switch(self):
        db = resolve(
            DATABASE_URL="postgres://u:p@h:5432/from_url",
            DJANGO_DB_ENGINE="sqlite3",
            DJANGO_DB_NAME="ignored.sqlite3",
        )
        self.assertEqual(db["ENGINE"], "django.db.backends.postgresql")
        self.assertEqual(db["NAME"], "from_url")

    def test_a_blank_url_falls_through_to_the_engine_switch(self):
        db = resolve(DATABASE_URL="   ", DJANGO_DB_ENGINE="sqlite3")
        self.assertEqual(db["ENGINE"], "django.db.backends.sqlite3")

    def test_a_non_postgres_url_is_refused(self):
        with self.assertRaises(ImproperlyConfigured):
            resolve(DATABASE_URL="mysql://u:p@h/db")

    def test_a_url_without_a_database_name_is_refused(self):
        with self.assertRaises(ImproperlyConfigured):
            resolve(DATABASE_URL="postgres://u:p@h:5432/")
