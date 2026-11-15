"""Versioned policy uploads and read grants scoped to verified gateway users."""
from __future__ import annotations

import re
import sqlite3
import unicodedata
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from uuid import UUID, uuid4

from app import hard_policy_data as evidence
from app.gateway_identity import owner_id

MAX_DOCUMENT_BYTES = 65_536
MAX_READERS = 32
_MAX_OWNER_DOCUMENTS = 40
_MAX_VERSIONS = 20
_MAX_PASSAGES = 40
_UNSUPPORTED_MARKUP = re.compile(r"<[!?/a-z]|^[ \t]{0,3}(?:```|~~~|>)|^(?: {4}|\t)[^\r\n]", re.I | re.M)
_METADATA_COLUMNS = "d.id, d.owner, d.source, v.version, v.title, v.filename, v.digest, v.created_at"
_ACCESS = "(d.owner = ? OR EXISTS (SELECT 1 FROM policy_document_readers r WHERE r.document_id = d.id AND r.reader = ?))"
_SCHEMA = """
CREATE TABLE IF NOT EXISTS policy_documents (
    id TEXT PRIMARY KEY,
    owner TEXT NOT NULL,
    source TEXT NOT NULL CHECK (source IN ('policy', 'bulletin')),
    latest INTEGER NOT NULL CHECK (latest >= 1)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS policy_document_owners ON policy_documents(owner);
CREATE TABLE IF NOT EXISTS policy_document_versions (
    document_id TEXT NOT NULL REFERENCES policy_documents(id) ON DELETE CASCADE,
    version INTEGER NOT NULL CHECK (version >= 1),
    title TEXT NOT NULL,
    filename TEXT NOT NULL,
    digest TEXT NOT NULL,
    created_at TEXT NOT NULL,
    content BLOB NOT NULL,
    PRIMARY KEY (document_id, version)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS policy_document_readers (
    document_id TEXT NOT NULL REFERENCES policy_documents(id) ON DELETE CASCADE,
    reader TEXT NOT NULL,
    user TEXT NOT NULL,
    PRIMARY KEY (document_id, reader)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS policy_document_reader_lookup ON policy_document_readers(reader, document_id);
"""


class PolicyDocumentMissing(LookupError):
    """The same result covers missing IDs and unauthorized requests."""


class PolicyDocumentInput(ValueError):
    pass


class PolicyDocumentLimit(ValueError):
    pass


def _validate(filename: str, content: bytes) -> tuple[str, str]:
    if (
        not 4 <= len(filename) <= 120 or filename != filename.strip() or filename.startswith(".")
        or not filename.casefold().endswith((".md", ".markdown"))
        or any(char in '/\\?#%<>"' or unicodedata.category(char).startswith("C") for char in filename)
    ):
        raise PolicyDocumentInput("请使用不含路径或特殊字符的 Markdown 文件名。")
    if not content or len(content) > MAX_DOCUMENT_BYTES:
        raise PolicyDocumentInput("Markdown 文件不能为空，且不得超过 64 KiB。")
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise PolicyDocumentInput("请上传 UTF-8 Markdown 文件。") from None
    if any(char not in "\t\r\n" and unicodedata.category(char).startswith("C") for char in text):
        raise PolicyDocumentInput("文档包含不支持的控制字符。")
    if _UNSUPPORTED_MARKUP.search(text):
        raise PolicyDocumentInput("请使用正文段落。文档不支持 HTML、引用块和代码块。")
    passages = evidence._passages(text.encode("utf-8"), "policy", filename)
    if not 1 <= len(passages) <= _MAX_PASSAGES or any(not 1 <= len(p.title) <= 120 or not p.quote.strip() for p in passages):
        raise PolicyDocumentInput("请在 1 至 40 个「## 标题」下填写政策或公告正文。")
    first = next(line for line in text.splitlines() if line.strip())
    title = first[2:].strip()[:120] if first.startswith("# ") and first[2:].strip() else passages[0].title
    return title, sha256(content).hexdigest()


class PolicyDocumentStore:
    def __init__(self, path: Path):
        self.path = path
        with closing(self._connect()) as connection, connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    @staticmethod
    def _metadata(row: sqlite3.Row, owner: str) -> dict:
        return {
            "id": row["id"], "title": row["title"], "filename": row["filename"],
            "source": row["source"], "version": row["version"], "owned": row["owner"] == owner,
            "sha256": row["digest"], "created_at": row["created_at"],
        }

    @staticmethod
    def _require(
        connection: sqlite3.Connection, owner: str, document_id: str, *, write: bool = False,
    ) -> sqlite3.Row:
        access = "d.owner = ?" if write else _ACCESS
        identities = (owner,) if write else (owner, owner)
        document = connection.execute(
            f"SELECT d.* FROM policy_documents d WHERE d.id = ? AND {access}",
            (document_id, *identities),
        ).fetchone()
        if document is None:
            raise PolicyDocumentMissing()
        return document

    @staticmethod
    def _current(
        connection: sqlite3.Connection, document_id: str, *, version: int | None = None, content: bool = False,
    ) -> sqlite3.Row:
        fields = _METADATA_COLUMNS + (", v.content" if content else "")
        selected = "d.latest" if version is None else "?"
        parameters = (document_id,) if version is None else (version, document_id)
        row = connection.execute(
            f"""SELECT {fields} FROM policy_documents d
                JOIN policy_document_versions v ON v.document_id = d.id AND v.version = {selected}
                WHERE d.id = ?""",
            parameters,
        ).fetchone()
        if row is None:
            raise PolicyDocumentMissing()
        return row

    @staticmethod
    def _insert(
        connection: sqlite3.Connection, document_id: str, version: int, title: str, filename: str,
        digest: str, content: bytes,
    ) -> None:
        connection.execute(
            """INSERT INTO policy_document_versions
               (document_id, version, title, filename, digest, created_at, content)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (document_id, version, title, filename, digest, datetime.now(UTC).isoformat(timespec="seconds"), content),
        )

    def list_visible(self, owner: str, *, limit: int, offset: int) -> list[dict]:
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN")
            rows = connection.execute(
                f"""SELECT {_METADATA_COLUMNS} FROM policy_documents d
                    JOIN policy_document_versions v ON v.document_id = d.id AND v.version = d.latest
                    WHERE {_ACCESS} ORDER BY v.created_at DESC, d.id LIMIT ? OFFSET ?""",
                (owner, owner, limit, offset),
            ).fetchall()
            readers: dict[str, list[str]] = {}
            for row in connection.execute(
                """SELECT r.document_id, r.user FROM policy_document_readers r
                   JOIN policy_documents d ON d.id = r.document_id
                   WHERE d.owner = ? ORDER BY r.user""", (owner,),
            ):
                readers.setdefault(row["document_id"], []).append(row["user"])
            return [
                {**self._metadata(row, owner), **({"readers": readers.get(row["id"], [])} if row["owner"] == owner else {})}
                for row in rows
            ]

    def create(self, owner: str, filename: str, source: evidence.DocumentSource, content: bytes) -> dict:
        title, digest = _validate(filename, content)
        document_id = str(uuid4())
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            count = connection.execute("SELECT count(*) FROM policy_documents WHERE owner = ?", (owner,)).fetchone()[0]
            if count >= _MAX_OWNER_DOCUMENTS:
                raise PolicyDocumentLimit("每个账号最多保存 40 份政策文档。")
            connection.execute("INSERT INTO policy_documents VALUES (?, ?, ?, 1)", (document_id, owner, source))
            self._insert(connection, document_id, 1, title, filename, digest, content)
            return self._metadata(self._current(connection, document_id), owner)

    def revise(self, owner: str, document_id: UUID, filename: str, content: bytes) -> dict:
        document_id = str(document_id)
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require(connection, owner, document_id, write=True)
            previous = self._current(connection, document_id)
            title, digest = _validate(filename, content)
            if (previous["filename"], previous["digest"]) == (filename, digest):
                return self._metadata(previous, owner)
            version = previous["version"] + 1
            if version > _MAX_VERSIONS:
                raise PolicyDocumentLimit("每份政策文档最多保存 20 个版本。")
            self._insert(connection, document_id, version, title, filename, digest, content)
            connection.execute("UPDATE policy_documents SET latest = ? WHERE id = ?", (version, document_id))
            return self._metadata(self._current(connection, document_id), owner)

    def versions(self, owner: str, document_id: UUID) -> list[dict]:
        document_id = str(document_id)
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN")
            self._require(connection, owner, document_id)
            rows = connection.execute(
                f"""SELECT {_METADATA_COLUMNS} FROM policy_document_versions v
                    JOIN policy_documents d ON d.id = v.document_id
                    WHERE d.id = ? ORDER BY v.version DESC""", (document_id,),
            )
            return [self._metadata(row, owner) for row in rows]

    def version(self, owner: str, document_id: UUID, version: int) -> dict:
        document_id = str(document_id)
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN")
            self._require(connection, owner, document_id)
            row = self._current(connection, document_id, version=version, content=True)
            return {**self._metadata(row, owner), "content": row["content"].decode("utf-8")}

    def replace_readers(self, owner: str, document_id: UUID, users: list[str]) -> list[str]:
        document_id = str(document_id)
        readers = [(owner_id(user), user) for user in sorted(set(users))]
        readers = [(reader, user) for reader, user in readers if reader != owner]
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require(connection, owner, document_id, write=True)
            connection.execute("DELETE FROM policy_document_readers WHERE document_id = ?", (document_id,))
            connection.executemany(
                "INSERT INTO policy_document_readers VALUES (?, ?, ?)",
                [(document_id, reader, user) for reader, user in readers],
            )
        return [user for _, user in readers]

    def snapshot(self, owner: str, document_ids: list[UUID]) -> evidence.DocumentSnapshot:
        """Pin access grants and latest versions together before assembling evidence."""
        ids = sorted({str(document_id) for document_id in document_ids})
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN")
            rows = []
            for document_id in ids:
                self._require(connection, owner, document_id)
                rows.append(self._current(connection, document_id, content=True))

        catalog_bytes = evidence._read("catalog.json")
        digest = sha256(b"policy-upload-v1\0" + catalog_bytes)
        passages = []
        doc_numbers = set()
        for row in rows:
            document_id, version, filename = row["id"], row["version"], row["filename"]
            digest.update(f"\0{document_id}:{version}:{row['source']}:{row['digest']}".encode("ascii"))
            location = f"uploads/{document_id}/v{version}/{filename}"
            content = row["content"].decode("utf-8-sig").encode("utf-8")
            for passage in evidence._passages(content, row["source"], filename):
                numbers = evidence._DOCUMENT_NUMBER.findall(passage.quote)
                if len(numbers) > 1 or (numbers and numbers[0] in doc_numbers):
                    raise PolicyDocumentInput("所选文档有重复的文号。请分别调查，或上传更正版本。")
                doc_numbers.update(numbers)
                passages.append(replace(passage, file=location))
        return evidence.DocumentSnapshot(
            version=digest.hexdigest(),
            catalog=tuple(evidence._catalog(catalog_bytes).items()),
            passages=tuple(passages),
        )
