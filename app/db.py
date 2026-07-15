"""保存層。

このアプリで永続化に触れるのはこのモジュールだけ。FastAPI 本体（main.py）は
SQLite を直接知らない。将来 Firestore 等へ移すときは、ここにある関数の中身だけ
差し替える。リポジトリパターンや抽象基底クラスは導入しない（YAGNI）。

本文（body）は一切加工せずプレーンテキストで保存する。作成日時（created_at）は
ISO 8601・UTC の文字列で保存し、機械可読性を保つ。編集に関わるカラムは持たない
（「一度保存した記録は編集できない」という思想を構造で担保する）。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

# nichijou.db はプロジェクト直下に置く（.gitignore で除外済み）。
DB_PATH = Path(__file__).resolve().parent.parent / "nichijou.db"

# 観察写真の実体を置くディレクトリ（.gitignore で除外。DB にはファイル名のみ持つ）。
PHOTOS_DIR = Path(__file__).resolve().parent.parent / "photos"


@dataclass(frozen=True)
class Entry:
    id: int
    body: str
    created_at: datetime  # tz-aware（UTC）。表示側でローカル時刻へ変換する。


@dataclass(frozen=True)
class Observation:
    id: int
    filename: str            # PHOTOS_DIR 配下の保存ファイル名（相対）。
    species: str | None      # 判定種名／分かる分類階級。判定失敗時は None。
    confidence: str | None   # 高／中／低。判定失敗時は None。
    comment: str | None      # 一言コメント。判定失敗時は None。
    status: str              # "done"（判定済）／"failed"（判定失敗）。
    taken_at: datetime       # 撮影日時（EXIF）。無ければ登録日時で代用。tz-aware（UTC）。
    created_at: datetime     # 登録日時。tz-aware（UTC）。


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """テーブルが無ければ作る。アプリ起動時に1回呼ぶ。"""
    with _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS entries (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                body        TEXT NOT NULL,
                created_at  TEXT NOT NULL
            )
            """
        )
        # 観察テーブル。entries とは独立。撮影/位置のうち位置は保存しない（日時のみ）。
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS observations (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                filename    TEXT NOT NULL,
                species     TEXT,
                confidence  TEXT,
                comment     TEXT,
                status      TEXT NOT NULL,
                taken_at    TEXT NOT NULL,
                created_at  TEXT NOT NULL
            )
            """
        )


def add_entry(body: str) -> None:
    """本文を1件保存する。created_at はサーバ側で UTC を打つ。"""
    created_at = datetime.now(timezone.utc).isoformat()
    with _connect() as conn:
        conn.execute(
            "INSERT INTO entries (body, created_at) VALUES (?, ?)",
            (body, created_at),
        )


def _row_to_entry(row: sqlite3.Row) -> Entry:
    return Entry(
        id=row["id"],
        body=row["body"],
        created_at=datetime.fromisoformat(row["created_at"]),
    )


def list_entries() -> list[Entry]:
    """全件を新しい順（created_at 降順、同時刻は id 降順）で返す。"""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id, body, created_at FROM entries ORDER BY created_at DESC, id DESC"
        ).fetchall()
    return [_row_to_entry(row) for row in rows]


# LIKE のワイルドカード（% _）と、下の ESCAPE 文字自身を打ち消すための変換表。
# 利用者の入力はあくまで「文字列」であって、パターン言語ではない。
_LIKE_ESCAPE = str.maketrans({"\\": "\\\\", "%": "\\%", "_": "\\_"})


def search_entries(q: str) -> list[Entry]:
    """本文に q を含む記録を新しい順で返す（部分一致）。

    LIKE で足りる規模（個人利用の数百〜数千件）なので FTS5 は入れない。
    ASCII の大文字小文字は SQLite の LIKE が既定で無視する。日本語は
    そもそも区別が無いので、これで用は足りる。"""
    pattern = f"%{q.translate(_LIKE_ESCAPE)}%"
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT id, body, created_at FROM entries
            WHERE body LIKE ? ESCAPE '\\'
            ORDER BY created_at DESC, id DESC
            """,
            (pattern,),
        ).fetchall()
    return [_row_to_entry(row) for row in rows]


def delete_entry(entry_id: int) -> None:
    """1件削除する。"""
    with _connect() as conn:
        conn.execute("DELETE FROM entries WHERE id = ?", (entry_id,))


# --- 観察（写真＋AI判定） -------------------------------------------------

def _row_to_observation(row: sqlite3.Row) -> Observation:
    return Observation(
        id=row["id"],
        filename=row["filename"],
        species=row["species"],
        confidence=row["confidence"],
        comment=row["comment"],
        status=row["status"],
        taken_at=datetime.fromisoformat(row["taken_at"]),
        created_at=datetime.fromisoformat(row["created_at"]),
    )


def add_observation(
    filename: str,
    species: str | None,
    confidence: str | None,
    comment: str | None,
    status: str,
    taken_at: datetime | None,
) -> None:
    """観察を1件保存する。created_at はサーバ側で UTC を打つ。
    taken_at（撮影日時）が無ければ登録日時で代用する。"""
    created = datetime.now(timezone.utc)
    taken = taken_at or created
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO observations
                (filename, species, confidence, comment, status, taken_at, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (filename, species, confidence, comment, status,
             taken.isoformat(), created.isoformat()),
        )


def list_observations() -> list[Observation]:
    """全件を新しい順（撮影日時 降順、同時刻は id 降順）で返す。"""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM observations ORDER BY taken_at DESC, id DESC"
        ).fetchall()
    return [_row_to_observation(row) for row in rows]


def get_observation(obs_id: int) -> Observation | None:
    """1件取得する（再判定で写真ファイル名が必要になる）。"""
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM observations WHERE id = ?", (obs_id,)
        ).fetchone()
    return _row_to_observation(row) if row else None


def update_observation_result(
    obs_id: int,
    species: str | None,
    confidence: str | None,
    comment: str | None,
    status: str,
) -> None:
    """AI 判定結果だけを上書きする（再判定用）。撮影/登録日時や写真は変えない。"""
    with _connect() as conn:
        conn.execute(
            "UPDATE observations SET species = ?, confidence = ?, comment = ?, status = ? WHERE id = ?",
            (species, confidence, comment, status, obs_id),
        )


def delete_observation(obs_id: int) -> str | None:
    """1件削除する。写真ファイルの実体削除は呼び出し側で行うため、削除した
    ファイル名を返す（行が無ければ None）。"""
    with _connect() as conn:
        row = conn.execute(
            "SELECT filename FROM observations WHERE id = ?", (obs_id,)
        ).fetchone()
        conn.execute("DELETE FROM observations WHERE id = ?", (obs_id,))
    return row["filename"] if row else None
