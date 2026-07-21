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
class Photo:
    """観察に属する写真1枚。同じ被写体を複数アングルで撮った束の一員。"""
    filename: str      # PHOTOS_DIR 配下の保存ファイル名（相対）。
    taken_at: datetime  # 撮影日時（EXIF）。写真ごとに持つ。tz-aware（UTC）。


@dataclass(frozen=True)
class Observation:
    id: int
    filename: str            # 代表写真のファイル名。photos[0] と同じ（後方互換用）。
    species: str | None      # 判定種名／分かる分類階級。判定失敗時は None。
    confidence: str | None   # 高／中／低。判定失敗時は None。
    comment: str | None      # 一言コメント。判定失敗時は None。
    status: str              # "done"（判定済）／"failed"（判定失敗）。
    taken_at: datetime       # 撮影日時（EXIF）。無ければ登録日時で代用。tz-aware（UTC）。
    created_at: datetime     # 登録日時。tz-aware（UTC）。
    observer_note: str | None = None  # 観察者が現場で添える任意メモ。無ければ None。
    photos: tuple[Photo, ...] = ()    # この観察の全写真（ordinal 昇順）。


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
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                filename      TEXT NOT NULL,
                species       TEXT,
                confidence    TEXT,
                comment       TEXT,
                status        TEXT NOT NULL,
                taken_at      TEXT NOT NULL,
                created_at    TEXT NOT NULL,
                observer_note TEXT
            )
            """
        )
        # 既存 DB への軽量マイグレーション。observer_note を後付けした（NULL 可）。
        # 既存レコードは NULL のままでよい。カラムが既にあれば何もしない。
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(observations)")}
        if "observer_note" not in cols:
            conn.execute("ALTER TABLE observations ADD COLUMN observer_note TEXT")

        # 観察写真テーブル。observations と 1 対多（1観察＝同じ被写体の複数アングル）。
        # observations.filename は代表写真として当面残すが、正はこちら。
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS observation_photos (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                observation_id INTEGER NOT NULL REFERENCES observations(id),
                photo_path     TEXT NOT NULL,
                taken_at       TEXT NOT NULL,
                ordinal        INTEGER NOT NULL
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_observation_photos_obs "
            "ON observation_photos (observation_id, ordinal)"
        )
        # 1対多化のマイグレーション。既存の各観察（写真1枚）を1行ずつ流し込む。
        # べき等性は「observation_photos が空のときだけ実行する」で担保する
        # （移行後は必ず1行以上あるので、再起動で二重登録されない）。
        empty = conn.execute(
            "SELECT NOT EXISTS (SELECT 1 FROM observation_photos)"
        ).fetchone()[0]
        if empty:
            conn.execute(
                """
                INSERT INTO observation_photos
                    (observation_id, photo_path, taken_at, ordinal)
                SELECT id, filename, taken_at, 0 FROM observations
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

def _row_to_observation(
    row: sqlite3.Row, photos: tuple[Photo, ...] = ()
) -> Observation:
    return Observation(
        id=row["id"],
        filename=row["filename"],
        species=row["species"],
        confidence=row["confidence"],
        comment=row["comment"],
        status=row["status"],
        taken_at=datetime.fromisoformat(row["taken_at"]),
        created_at=datetime.fromisoformat(row["created_at"]),
        observer_note=row["observer_note"],
        photos=photos,
    )


def _photos_for(conn: sqlite3.Connection, obs_ids: list[int]) -> dict[int, list[Photo]]:
    """観察 id の集合について、写真を ordinal 昇順でまとめて引く（N+1 を避ける）。"""
    if not obs_ids:
        return {}
    marks = ",".join("?" * len(obs_ids))
    rows = conn.execute(
        f"SELECT observation_id, photo_path, taken_at FROM observation_photos "
        f"WHERE observation_id IN ({marks}) ORDER BY observation_id, ordinal, id",
        tuple(obs_ids),
    ).fetchall()
    out: dict[int, list[Photo]] = {}
    for r in rows:
        out.setdefault(r["observation_id"], []).append(
            Photo(filename=r["photo_path"], taken_at=datetime.fromisoformat(r["taken_at"]))
        )
    return out


def add_observation(
    photos: list[tuple[str, datetime | None]],
    species: str | None,
    confidence: str | None,
    comment: str | None,
    status: str,
    observer_note: str | None = None,
) -> int:
    """観察を1件保存する。photos は (ファイル名, 撮影日時) の並び（表示順）で、
    すべて同じ被写体を写したものとして1つの観察にまとまる。created_at はサーバ側で
    UTC を打つ。観察の taken_at は先頭写真の撮影日時（無ければ登録日時）を採る。
    observer_note は観察者が現場で添える任意メモ（空なら None）。新しい id を返す。"""
    if not photos:
        raise ValueError("観察には写真が最低1枚必要")
    created = datetime.now(timezone.utc)
    taken = photos[0][1] or created
    with _connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO observations
                (filename, species, confidence, comment, status, taken_at, created_at,
                 observer_note)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (photos[0][0], species, confidence, comment, status,
             taken.isoformat(), created.isoformat(), observer_note),
        )
        obs_id = cur.lastrowid
        conn.executemany(
            "INSERT INTO observation_photos "
            "(observation_id, photo_path, taken_at, ordinal) VALUES (?, ?, ?, ?)",
            [
                (obs_id, name, (shot or created).isoformat(), i)
                for i, (name, shot) in enumerate(photos)
            ],
        )
    return obs_id


def list_observations() -> list[Observation]:
    """全件を新しい順（撮影日時 降順、同時刻は id 降順）で、写真つきで返す。"""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM observations ORDER BY taken_at DESC, id DESC"
        ).fetchall()
        photos = _photos_for(conn, [r["id"] for r in rows])
    return [
        _row_to_observation(row, tuple(photos.get(row["id"], []))) for row in rows
    ]


def recent_observations_for_context(
    limit: int = 20, exclude_id: int | None = None
) -> list[tuple[datetime, str]]:
    """AI 判定時にプロンプトへ添える「直近の観察履歴」を新しい順で返す。

    種名が確定した観察（status='done' かつ species あり）だけを、撮影日時と
    種名のペアで返す。写真や本文は渡さない（プロンプトを短く保つ）。判定失敗の
    行や種名 None は履歴として無意味なので除く。exclude_id を渡すと、その観察を
    履歴から外す（再判定で自分自身が履歴に混ざるのを防ぐ）。

    種名は AI が返す文字列そのままなので、突合（同種の再登場かどうか）はここでは
    せず、この一覧を見た判定モデルに委ねる（無理な正規化はしない方針）。"""
    sql = (
        "SELECT taken_at, species FROM observations "
        "WHERE status = 'done' AND species IS NOT NULL AND species != ''"
    )
    params: list[object] = []
    if exclude_id is not None:
        sql += " AND id != ?"
        params.append(exclude_id)
    sql += " ORDER BY taken_at DESC, id DESC LIMIT ?"
    params.append(limit)
    with _connect() as conn:
        rows = conn.execute(sql, tuple(params)).fetchall()
    return [(datetime.fromisoformat(r["taken_at"]), r["species"]) for r in rows]


def get_observation(obs_id: int) -> Observation | None:
    """1件取得する（再判定でこの観察の全写真が必要になる）。"""
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM observations WHERE id = ?", (obs_id,)
        ).fetchone()
        if row is None:
            return None
        photos = _photos_for(conn, [obs_id])
    return _row_to_observation(row, tuple(photos.get(obs_id, [])))


def update_observation_result(
    obs_id: int,
    species: str | None,
    confidence: str | None,
    comment: str | None,
    status: str,
    observer_note: str | None = None,
) -> None:
    """AI 判定結果を上書きする（再判定用）。撮影/登録日時や写真は変えない。
    observer_note も一緒に上書きする（再判定フォームで修正できるため）。"""
    with _connect() as conn:
        conn.execute(
            "UPDATE observations SET species = ?, confidence = ?, comment = ?, "
            "status = ?, observer_note = ? WHERE id = ?",
            (species, confidence, comment, status, observer_note, obs_id),
        )


def delete_observation(obs_id: int) -> list[str]:
    """1件削除する。写真ファイルの実体削除は呼び出し側で行うため、削除した
    ファイル名を全て返す（行が無ければ空リスト）。"""
    with _connect() as conn:
        row = conn.execute(
            "SELECT filename FROM observations WHERE id = ?", (obs_id,)
        ).fetchone()
        if row is None:
            return []
        names = [
            r["photo_path"]
            for r in conn.execute(
                "SELECT photo_path FROM observation_photos WHERE observation_id = ?",
                (obs_id,),
            )
        ]
        conn.execute("DELETE FROM observation_photos WHERE observation_id = ?", (obs_id,))
        conn.execute("DELETE FROM observations WHERE id = ?", (obs_id,))
    # 代表写真は observation_photos にも入っているのが正だが、念のため取りこぼさない。
    if row["filename"] not in names:
        names.append(row["filename"])
    return names
