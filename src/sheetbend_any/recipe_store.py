"""Private transactional recipe storage. Code is immutable; authority is never cached."""

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
import os
from pathlib import Path
import re
import sqlite3
import stat
import time
from typing import Any

from .contracts import canonical_json, parse_json, validate_document
from .errors import CacheError
from .recipe import Recipe
from .request import PathLike

_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,63}\Z")
_DDL = (
    "CREATE TABLE IF NOT EXISTS recipes (digest TEXT PRIMARY KEY, document TEXT NOT NULL, provenance TEXT NOT NULL, created REAL NOT NULL)",
    "CREATE TABLE IF NOT EXISTS requests (key TEXT PRIMARY KEY, digest TEXT NOT NULL REFERENCES recipes(digest) ON DELETE CASCADE)",
    "CREATE TABLE IF NOT EXISTS names (name TEXT PRIMARY KEY, digest TEXT NOT NULL REFERENCES recipes(digest) ON DELETE CASCADE)",
    "CREATE TABLE IF NOT EXISTS qualifications (digest TEXT NOT NULL REFERENCES recipes(digest) ON DELETE CASCADE, runtime TEXT NOT NULL, tests INTEGER NOT NULL, PRIMARY KEY(digest, runtime))",
    "CREATE TABLE IF NOT EXISTS runs (id INTEGER PRIMARY KEY, record TEXT NOT NULL)",
)


def default_cache_directory() -> Path:
    xdg = os.environ.get("XDG_CACHE_HOME", "")
    base = Path(xdg) if xdg and Path(xdg).is_absolute() else Path.home() / ".cache"
    return base / "any"


class RecipeStore:
    """Lazy private SQLite cache with at most 256 unnamed recipes and 100 run receipts.

    Named recipes are retained until the user removes the cache; they are not
    evicted. Stored programs/descriptions can themselves contain sensitive text.
    The cache is not a secret vault or an authenticity/signature mechanism.
    """

    def __init__(self, directory: PathLike | None = None, *, max_unnamed: int = 256) -> None:
        self.directory = Path(directory).expanduser().absolute() if directory is not None else default_cache_directory()
        if type(max_unnamed) is not int or max_unnamed < 1:
            raise ValueError("max_unnamed must be a positive integer.")
        self.max_unnamed = max_unnamed

    @contextmanager
    def _transaction(self, *, create: bool = True) -> Iterator[sqlite3.Connection | None]:
        connection = None
        try:
            if create:
                self.directory.parent.mkdir(parents=True, exist_ok=True)
                self.directory.mkdir(mode=0o700, exist_ok=True)
            try:
                info = self.directory.lstat()
            except FileNotFoundError:
                yield None
                return
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise CacheError("Cache directory must be a private, user-owned, non-symlink directory (0700).")
            path = self.directory / "recipes.sqlite3"
            if create:
                try:
                    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
                except FileExistsError:
                    pass
                else:
                    os.close(fd)
            try:
                info = path.lstat()
            except FileNotFoundError:
                yield None
                return
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_mode & 0o077 or info.st_nlink != 1):
                raise CacheError("Cache database must be a private, singly linked regular file (0600).")
            connection = sqlite3.connect(path, timeout=5)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("BEGIN IMMEDIATE" if create else "BEGIN")
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1):
                raise CacheError("Unsupported cache database version.")
            if version == 0:
                if not create:
                    raise CacheError("Cache has no recognized schema.")
                for statement in _DDL:
                    connection.execute(statement)
                connection.execute("PRAGMA user_version=1")
            yield connection
            connection.commit()
        except (OSError, sqlite3.Error) as exc:
            raise CacheError("Recipe cache access failed; no corrupt-cache fallback was used.") from exc
        finally:
            if connection is not None:
                connection.close()

    @staticmethod
    def _decode(row: sqlite3.Row) -> Recipe:
        if len(row["document"].encode("utf-8")) > 262144:
            raise CacheError("Cached recipe exceeds its document limit.")
        recipe = Recipe.from_json(row["document"])
        if recipe.digest != row["digest"]:
            raise CacheError("Cached recipe digest does not match its contents.")
        return recipe

    def find(self, key: str) -> Recipe | None:
        """Look up an exact request without creating a missing cache or contacting a model."""
        with self._transaction(create=False) as db:
            if db is None:
                return None
            row = db.execute("SELECT r.digest,r.document FROM recipes r JOIN requests q ON q.digest=r.digest WHERE q.key=?", (key,)).fetchone()
            return None if row is None else self._decode(row)

    def get(self, name_or_digest: str) -> Recipe:
        """Load a literal saved name or full digest, never a fuzzy or prefix match."""
        with self._transaction(create=False) as db:
            if db is not None:
                if _DIGEST.fullmatch(name_or_digest):
                    row = db.execute("SELECT digest,document FROM recipes WHERE digest=?", (name_or_digest,)).fetchone()
                else:
                    row = db.execute("SELECT r.digest,r.document FROM recipes r JOIN names n ON n.digest=r.digest WHERE n.name=?", (name_or_digest,)).fetchone()
                if row is not None:
                    return self._decode(row)
        raise CacheError(f"No saved recipe named {name_or_digest!r}.")

    def put(self, recipe: Recipe, *, provenance: Mapping[str, Any] | None = None) -> None:
        """Store immutable canonical JSON. Qualification is recorded separately."""
        document = canonical_json(recipe.to_dict()).decode("utf-8")
        origin = canonical_json(dict(provenance or {})).decode("utf-8")
        with self._transaction() as db:
            row = db.execute("SELECT digest,document FROM recipes WHERE digest=?", (recipe.digest,)).fetchone()
            if row is not None:
                self._decode(row)
            else:
                db.execute("INSERT INTO recipes VALUES (?,?,?,?)", (recipe.digest, document, origin, time.time()))
            db.execute(
                "DELETE FROM recipes WHERE digest IN (SELECT digest FROM recipes WHERE digest NOT IN (SELECT digest FROM names) ORDER BY created DESC, digest LIMIT -1 OFFSET ?)",
                (self.max_unnamed,),
            )

    def remember(self, key: str, digest: str, *, replace: bool = False) -> str:
        """First concurrent generation wins unless regeneration was explicitly requested."""
        with self._transaction() as db:
            if replace:
                db.execute("INSERT INTO requests VALUES (?,?) ON CONFLICT(key) DO UPDATE SET digest=excluded.digest", (key, digest))
            else:
                db.execute("INSERT OR IGNORE INTO requests VALUES (?,?)", (key, digest))
            return db.execute("SELECT digest FROM requests WHERE key=?", (key,)).fetchone()[0]

    def save_name(self, name: str, recipe: Recipe) -> None:
        """Name an existing recipe; task --overwrite never permits replacing a saved name."""
        if not _NAME.fullmatch(name) or _DIGEST.fullmatch(name):
            raise CacheError("Recipe name must start with a letter and contain at most 64 letters, digits, dots, dashes, or underscores.")
        self.put(recipe)
        with self._transaction() as db:
            previous = db.execute("SELECT digest FROM names WHERE name=?", (name,)).fetchone()
            if previous is not None and previous[0] != recipe.digest:
                raise CacheError(f"Recipe name already refers to another immutable program: {name}")
            db.execute("INSERT OR IGNORE INTO names VALUES (?,?)", (name, recipe.digest))

    def qualification(self, recipe: Recipe, runtime: str) -> int | None:
        with self._transaction(create=False) as db:
            if db is None:
                return None
            row = db.execute("SELECT tests FROM qualifications WHERE digest=? AND runtime=?", (recipe.digest, runtime)).fetchone()
            return None if row is None else row[0]

    def qualify(self, recipe: Recipe, runtime: str, tests: int) -> None:
        if tests != len(recipe.to_dict()["tests"]):
            raise CacheError("Qualification must cover every fixture.")
        self.put(recipe)
        with self._transaction() as db:
            db.execute("INSERT OR REPLACE INTO qualifications VALUES (?,?,?)", (recipe.digest, runtime, tests))

    def record_run(self, record: Mapping[str, Any]) -> None:
        validate_document(dict(record), "run")
        with self._transaction() as db:
            db.execute("INSERT INTO runs(record) VALUES (?)", (canonical_json(record).decode("utf-8"),))
            db.execute("DELETE FROM runs WHERE id NOT IN (SELECT id FROM runs ORDER BY id DESC LIMIT 100)")

    def runs(self) -> list[dict[str, Any]]:
        with self._transaction(create=False) as db:
            if db is None:
                return []
            return [parse_json(row[0]) for row in db.execute("SELECT record FROM runs ORDER BY id DESC")]

    def entries(self) -> list[dict[str, Any]]:
        """Small inventory of cached programs and saved names, without executing any of them."""
        with self._transaction(create=False) as db:
            if db is None:
                return []
            rows = db.execute("SELECT r.digest,r.document,n.name FROM recipes r LEFT JOIN names n ON n.digest=r.digest ORDER BY r.created DESC,n.name")
            return [{"digest": row["digest"], "name": row["name"], "description": self._decode(row).description} for row in rows]

    def __repr__(self) -> str:
        return f"RecipeStore(directory={str(self.directory)!r}, max_unnamed={self.max_unnamed})"
