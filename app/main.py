"""日乗（nichijou）— FastAPI アプリ本体。

画面は1枚（index.html）のみ。開いてすぐ書ける、を最優先にする。
編集ルートは存在しない（PUT/PATCH なし）。これは思想上の核。
"""

from __future__ import annotations

import logging
import os
import re
import secrets as _secrets
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from PIL import Image

from . import db, vision

# --- 投函口（LAN 経由アップロード）の設定 -----------------------------------
# token は secrets/upload.token（生の 64 hex 一行）から直読みする。env 注入に
# 依存しないので systemd / start.sh / uv run のどの起動でも同じに動く。無ければ
# 投函口は全て 404（fail-closed。トークンを置かない限り投函口は存在しない）。
_UPLOAD_TOKEN_PATH = Path(__file__).resolve().parent.parent / "secrets" / "upload.token"

# 1回の投函（＝1つの観察）に含められる写真の上限。同じ被写体を複数アングルで
# 撮る用途に対して常識的な枚数。判定は全画像を1コールに載せるので、青天井にすると
# 入力トークンとレイテンシがそのまま膨らむ。
_MAX_PHOTOS_PER_OBSERVATION = 8

# 投函口の受理上限（LAN 露出面なので常識的な値で明示的に絞る）。
_UPLOAD_MAX_TOTAL_BYTES = 64 * 1024 * 1024  # 64 MiB

# このインスタンスが「LAN 公開の投函口専用」かどうか（環境変数で切り替える）。
#
# セキュリティ境界は「どのポートを LAN に転送するか」で引く（送信元 IP 判定はしない）。
# WSL2 は NAT 配下なので、Windows が portproxy で転送したポートだけが LAN から届く。
# 理由: Windows ホスト経由の接続（localhost 転送）も LAN のスマホ（portproxy）も、
# WSL アプリから見た送信元は同じゲートウェイ IP になり、両者を区別できないため。
#
#   - 日乗インスタンス:  127.0.0.1 にバインドし LAN へ転送しない → PC 専用（NAT が隔離）
#   - 投函口インスタンス: 0.0.0.0 にバインドし該当ポートだけ転送。NICHIJOU_LAN_ONLY=1 を
#                         付け、/u/ 以外は 404（母屋はこのインスタンスに存在しない）
_LAN_ONLY = os.environ.get("NICHIJOU_LAN_ONLY") == "1"


def _load_upload_token() -> str | None:
    """投函口トークンを読む。無い/空なら None（投函口を無効化する）。"""
    try:
        token = _UPLOAD_TOKEN_PATH.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return token or None


# 起動時に一度だけ読んでメモリ保持する。
_UPLOAD_TOKEN = _load_upload_token()


def _token_ok(token: str) -> bool:
    """URL の token が設定トークンと一致するか（タイミング攻撃対策込み）。
    トークン未設定なら常に不一致扱い（投函口は存在しないものとして 404）。"""
    if not _UPLOAD_TOKEN:
        return False
    return _secrets.compare_digest(token, _UPLOAD_TOKEN)


# --- token をログに残さない ---------------------------------------------------
# token は URL パスに含まれ、uvicorn のアクセスログに出うる。ログレコードの
# 引数中の /u/<hex...> を伏字化するフィルタを uvicorn.access に装着する。
class _RedactUploadToken(logging.Filter):
    _RE = re.compile(r"/u/[0-9a-fA-F]{16,}")

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003
        if record.args:
            record.args = tuple(
                self._RE.sub("/u/<redacted>", a) if isinstance(a, str) else a
                for a in record.args
            )
        return True


logging.getLogger("uvicorn.access").addFilter(_RedactUploadToken())


app = FastAPI(title="日乗 nichijou")


@app.middleware("http")
async def _drop_box_only(request: Request, call_next):
    """LAN 公開の投函口インスタンス（NICHIJOU_LAN_ONLY=1）では、投函口 /u/ 以外の
    全ルート（/ , /entries* , /observations* , /photos/*）を 404 で塞ぐ。母屋は
    このインスタンスには存在しないものとして扱い、日記の存在自体を晒さない。

    日乗インスタンス（127.0.0.1 バインド・LAN 非公開）では制限しない。PC 専用で
    ある保証はバインド先と「LAN へ転送しないこと」で担保する（NAT による隔離）。"""
    if _LAN_ONLY and not request.url.path.startswith("/u/"):
        return PlainTextResponse("not found", status_code=404)
    return await call_next(request)

templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent / "templates"))

# 写真の保存先を用意し、/photos で配信する（StaticFiles は Starlette 内蔵）。
db.PHOTOS_DIR.mkdir(exist_ok=True)
app.mount("/photos", StaticFiles(directory=str(db.PHOTOS_DIR)), name="photos")

# 受理する画像形式（content-type → 拡張子）。これ以外は黙って無視する。
_ALLOWED_IMAGES = {"image/jpeg": ".jpg", "image/png": ".png"}

# EXIF タグ番号。撮影日時だけを読む。GPS（0x8825）は一切触らない（プライバシー）。
_EXIF_IFD = 0x8769           # Exif サブ IFD の入口。
_DATETIME_ORIGINAL = 0x9003  # DateTimeOriginal。


def _format_local(dt) -> str:
    """UTC で保存された日時を、サーバのローカル時刻に変換して表示用に整形する。"""
    return dt.astimezone().strftime("%Y-%m-%d %H:%M")


templates.env.filters["local"] = _format_local


def _clean_note(note: str | None) -> str | None:
    """フォームの観察者メモを正規化する。空白のみ／未入力は None に落とす
    （DB を「メモ無し＝NULL」で揃え、判定プロンプトも従来と同一に保つ）。"""
    if note is None:
        return None
    note = note.strip()
    return note or None


def _save_photo(data: bytes, ext: str) -> str:
    """写真をサーバ採番のファイル名で保存し、そのファイル名を返す。
    原名は使わない（衝突回避・パストラバーサル無効化）。"""
    name = f"{datetime.now(timezone.utc):%Y%m%dT%H%M%S}-{uuid4().hex[:8]}{ext}"
    (db.PHOTOS_DIR / name).write_bytes(data)
    return name


def _read_taken_at(path: Path) -> datetime | None:
    """EXIF の撮影日時（DateTimeOriginal）だけを読む。GPS は読まない。
    読めなければ None を返す。EXIF 時刻はタイムゾーンを持たないため、サーバの
    ローカル時刻とみなして UTC へ変換する（表示側で再びローカルへ戻る）。"""
    try:
        with Image.open(path) as img:
            exif = img.getexif()
            raw = exif.get_ifd(_EXIF_IFD).get(_DATETIME_ORIGINAL)
        if not raw:
            return None
        naive = datetime.strptime(str(raw), "%Y:%m:%d %H:%M:%S")
        return naive.astimezone().astimezone(timezone.utc)
    except Exception:
        return None


def _media_type_of(filename: str) -> str:
    """保存済みファイル名から media_type を引く（拡張子はこちらで採番している）。"""
    return "image/png" if filename.endswith(".png") else "image/jpeg"


def _ingest_observation(
    files: list[tuple[bytes, str | None]], observer_note: str | None = None
) -> tuple[str | None, int]:
    """1回の投函を 保存→EXIF→AI判定→DB へ取り込む。観察の取り込み処理の実体。

    files に複数枚あれば「同じ被写体の複数アングル」とみなし、1つの観察として
    登録して1回の判定にまとめて渡す（1投函＝1観察）。戻り値は
    (status, 対象外だった枚数)。status は "done"／"failed"、jpg/png が1枚も
    無ければ None（観察を作らない）。観察ルートと投函口の両方がこの経路を共有する。
    observer_note（任意の現場メモ）は判定と保存の両方に渡す（空なら None）。"""
    saved: list[tuple[str, datetime | None]] = []  # (ファイル名, 撮影日時)
    images: list[tuple[bytes, str]] = []           # (バイト列, media_type)
    skipped = 0
    for data, content_type in files:
        ext = _ALLOWED_IMAGES.get(content_type or "")
        if ext is None:
            # jpg/png 以外は取り込まない（既存の拡張子制限）。
            skipped += 1
            continue
        filename = _save_photo(data, ext)
        saved.append((filename, _read_taken_at(db.PHOTOS_DIR / filename)))
        images.append((data, content_type))
    if not saved:
        return None, skipped

    # 観察の撮影日時は先頭写真のものを使う（db.add_observation 側の規約）。
    taken_at = saved[0][1]
    # 直近の観察履歴（日付＋種名）を判定へ渡す。この観察はまだ DB に無いので
    # 除外は不要。履歴が空（初観察）でも classify は従来どおり動く。
    history = db.recent_observations_for_context()
    try:
        j = vision.classify(images, taken_at, history, observer_note)
        db.add_observation(
            saved, j.species, j.confidence, j.comment, "done", observer_note
        )
        return "done", skipped
    except Exception:
        # 判定に失敗しても写真は残す。後から再判定でやり直せる。
        db.add_observation(saved, None, None, None, "failed", observer_note)
        return "failed", skipped


@app.on_event("startup")
def _startup() -> None:
    db.init_db()


@app.get("/")
def index(request: Request):
    entries = db.list_entries()
    return templates.TemplateResponse(
        request, "index.html", {"entries": entries}
    )


@app.post("/entries")
def create_entry(body: str = Form(...)):
    # 空白のみの投稿は保存しない（摩擦を増やさないため、エラーにはせず黙って戻す）。
    if body.strip():
        db.add_entry(body)
    return RedirectResponse(url="/", status_code=303)


@app.post("/entries/{entry_id}/delete")
def remove_entry(entry_id: int):
    db.delete_entry(entry_id)
    return RedirectResponse(url="/", status_code=303)


@app.get("/search")
def search(request: Request, q: str = ""):
    """記録を読み返すための検索。書き換えはしない（読むだけのルート）。
    q が空なら検索せず、フォームだけを出す。"""
    q = q.strip()
    entries = db.search_entries(q) if q else []
    return templates.TemplateResponse(request, "search.html", {"q": q, "entries": entries})


# --- 観察（写真＋AI判定） -------------------------------------------------

@app.get("/observations")
def observations(request: Request):
    return templates.TemplateResponse(
        request, "observations.html", {"observations": db.list_observations()}
    )


@app.post("/observations")
async def create_observations(
    photos: list[UploadFile] = File(...),
    observer_note: str = Form(""),
):
    # 1回の取り込み＝1つの観察。複数枚は「同じ被写体の複数アングル」として
    # まとめて1回の判定に渡す。別の被写体は分けて取り込む（UI に明記）。
    if len(photos) > _MAX_PHOTOS_PER_OBSERVATION:
        return PlainTextResponse(
            f"1つの観察に使える写真は {_MAX_PHOTOS_PER_OBSERVATION} 枚までです",
            status_code=413,
        )
    note = _clean_note(observer_note)
    files = [(await photo.read(), photo.content_type) for photo in photos]
    # jpg/png 以外は _ingest_observation が対象外として落とす（従来どおり）。
    _ingest_observation(files, note)
    return RedirectResponse(url="/observations", status_code=303)


@app.post("/observations/{obs_id}/reclassify")
def reclassify_observation(obs_id: int, observer_note: str = Form("")):
    # 再判定時はメモを保存し直した上で判定に渡す（フォームで修正できる）。
    note = _clean_note(observer_note)
    obs = db.get_observation(obs_id)
    if obs is not None:
        # その観察に紐づく全写真を渡し直す（取り込み時と同じ材料で判定させる）。
        names = [p.filename for p in obs.photos] or [obs.filename]
        try:
            images = [
                ((db.PHOTOS_DIR / name).read_bytes(), _media_type_of(name))
                for name in names
            ]
            # 再判定では自分自身を履歴から外す（自分を「再登場」と誤読させない）。
            history = db.recent_observations_for_context(exclude_id=obs_id)
            j = vision.classify(images, obs.taken_at, history, note)
            db.update_observation_result(
                obs_id, j.species, j.confidence, j.comment, "done", note
            )
        except Exception:
            db.update_observation_result(obs_id, None, None, None, "failed", note)
    return RedirectResponse(url="/observations", status_code=303)


@app.post("/observations/{obs_id}/delete")
def remove_observation(obs_id: int):
    for filename in db.delete_observation(obs_id):
        (db.PHOTOS_DIR / filename).unlink(missing_ok=True)
    return RedirectResponse(url="/observations", status_code=303)


# --- 投函口（スマホから LAN 経由で写真を投函する専用ルート） -----------------
# ミドルウェアにより /u/ 配下だけが非ループバックからも到達できる。ここで token を
# 照合し、不一致は 404（存在自体を隠す）。一覧・閲覧は一切提供しない（入れるだけ）。

def _drop_page(request: Request, token: str, result: dict | None):
    """投函ページを描画する。result が None なら投函フォームのみ。"""
    return templates.TemplateResponse(
        request, "upload.html", {"token": token, "result": result}
    )


@app.get("/u/{token}")
def drop_form(request: Request, token: str):
    if not _token_ok(token):
        return PlainTextResponse("not found", status_code=404)
    return _drop_page(request, token, result=None)


@app.post("/u/{token}")
async def drop_submit(
    request: Request,
    token: str,
    photos: list[UploadFile] = File(...),
    observer_note: str = Form(""),
):
    if not _token_ok(token):
        return PlainTextResponse("not found", status_code=404)

    # 1回の投函＝1つの観察。複数枚は「同じ被写体の複数アングル」として扱う。
    # メモも当然その1観察に付く。
    note = _clean_note(observer_note)

    # 上限チェック（読み込み前に。メモリ枯渇と乱用を防ぐ）。
    if len(photos) > _MAX_PHOTOS_PER_OBSERVATION:
        return PlainTextResponse(
            f"枚数が多すぎます（1つの観察につき上限 {_MAX_PHOTOS_PER_OBSERVATION} 枚）",
            status_code=413,
        )
    content_length = request.headers.get("content-length")
    if content_length is not None and content_length.isdigit():
        if int(content_length) > _UPLOAD_MAX_TOTAL_BYTES:
            return PlainTextResponse(
                f"合計サイズが大きすぎます（上限 {_UPLOAD_MAX_TOTAL_BYTES // (1024 * 1024)} MiB）",
                status_code=413,
            )

    files: list[tuple[bytes, str | None]] = []
    total = 0
    for photo in photos:
        data = await photo.read()
        # Content-Length が無い/欺いた場合の保険。累積で上限を超えたら打ち切る。
        total += len(data)
        if total > _UPLOAD_MAX_TOTAL_BYTES:
            return PlainTextResponse(
                f"合計サイズが大きすぎます（上限 {_UPLOAD_MAX_TOTAL_BYTES // (1024 * 1024)} MiB）",
                status_code=413,
            )
        files.append((data, photo.content_type))

    status, skipped = _ingest_observation(files, note)
    # 投函口は一覧が無いので、対象外（jpg/png 以外）の枚数もここで知らせる。
    result = {
        "accepted": len(files) - skipped,
        "status": status,
        "skipped": skipped,
    }
    return _drop_page(request, token, result=result)
