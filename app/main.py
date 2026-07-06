"""日乗（nichijou）— FastAPI アプリ本体。

画面は1枚（index.html）のみ。開いてすぐ書ける、を最優先にする。
編集ルートは存在しない（PUT/PATCH なし）。これは思想上の核。
"""

from __future__ import annotations

import logging
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

# 投函口の受理上限（LAN 露出面なので常識的な値で明示的に絞る）。
_UPLOAD_MAX_FILES = 20
_UPLOAD_MAX_TOTAL_BYTES = 64 * 1024 * 1024  # 64 MiB

# 母屋（日乗・観察・写真閲覧）へアクセスを許すのはループバックのみ。
# 0.0.0.0 バインドでは実際には 127.0.0.1 だけが該当する（::1 は保険）。
_LOOPBACK_HOSTS = {"127.0.0.1", "::1"}


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
async def _restrict_to_loopback(request: Request, call_next):
    """母屋（/ , /entries* , /observations* , /photos/*）は 127.0.0.1 からのみ。
    投函口（/u/ 配下）だけは非ループバックでも通す（token 照合は各ハンドラが行う）。

    判定は必ずソケットの実ピア request.client.host のみを見る。X-Forwarded-For
    等の転送ヘッダは一切信用しない（netsh portproxy は生 TCP 転送で XFF を付けない
    し、付いていても無視する）。WSL2 は NAT 配下のため LAN 由来の接続は必ず 172.x
    などになり、127.0.0.1 になることはない。"""
    if not request.url.path.startswith("/u/"):
        client = request.client.host if request.client else None
        if client not in _LOOPBACK_HOSTS:
            # fail-closed: client が取れない異常時もここで弾く。
            return PlainTextResponse("forbidden", status_code=403)
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


def _ingest_one(data: bytes, content_type: str | None) -> str | None:
    """写真1枚を 保存→EXIF→AI判定→DB へ取り込む。観察の取り込み処理の実体。
    戻り値は status（"done"／"failed"）。jpg/png 以外は取り込まず None（対象外）。
    観察ルートと投函口の両方がこの経路を共有する。"""
    ext = _ALLOWED_IMAGES.get(content_type or "")
    if ext is None:
        # jpg/png 以外は取り込まない（既存の拡張子制限）。
        return None
    filename = _save_photo(data, ext)
    taken_at = _read_taken_at(db.PHOTOS_DIR / filename)
    try:
        j = vision.classify(data, content_type)
        db.add_observation(filename, j.species, j.confidence, j.comment, "done", taken_at)
        return "done"
    except Exception:
        # 判定に失敗しても写真は残す。後から再判定でやり直せる。
        db.add_observation(filename, None, None, None, "failed", taken_at)
        return "failed"


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


# --- 観察（写真＋AI判定） -------------------------------------------------

@app.get("/observations")
def observations(request: Request):
    return templates.TemplateResponse(
        request, "observations.html", {"observations": db.list_observations()}
    )


@app.post("/observations")
async def create_observations(photos: list[UploadFile] = File(...)):
    for photo in photos:
        data = await photo.read()
        # jpg/png 以外は _ingest_one が None を返す＝黙って無視（従来どおり）。
        _ingest_one(data, photo.content_type)
    return RedirectResponse(url="/observations", status_code=303)


@app.post("/observations/{obs_id}/reclassify")
def reclassify_observation(obs_id: int):
    obs = db.get_observation(obs_id)
    if obs is not None:
        media_type = "image/png" if obs.filename.endswith(".png") else "image/jpeg"
        try:
            data = (db.PHOTOS_DIR / obs.filename).read_bytes()
            j = vision.classify(data, media_type)
            db.update_observation_result(obs_id, j.species, j.confidence, j.comment, "done")
        except Exception:
            db.update_observation_result(obs_id, None, None, None, "failed")
    return RedirectResponse(url="/observations", status_code=303)


@app.post("/observations/{obs_id}/delete")
def remove_observation(obs_id: int):
    filename = db.delete_observation(obs_id)
    if filename:
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
async def drop_submit(request: Request, token: str, photos: list[UploadFile] = File(...)):
    if not _token_ok(token):
        return PlainTextResponse("not found", status_code=404)

    # 上限チェック（読み込み前に。メモリ枯渇と乱用を防ぐ）。
    if len(photos) > _UPLOAD_MAX_FILES:
        return PlainTextResponse(
            f"枚数が多すぎます（上限 {_UPLOAD_MAX_FILES} 枚）", status_code=413
        )
    content_length = request.headers.get("content-length")
    if content_length is not None and content_length.isdigit():
        if int(content_length) > _UPLOAD_MAX_TOTAL_BYTES:
            return PlainTextResponse(
                f"合計サイズが大きすぎます（上限 {_UPLOAD_MAX_TOTAL_BYTES // (1024 * 1024)} MiB）",
                status_code=413,
            )

    done = failed = skipped = 0
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
        status = _ingest_one(data, photo.content_type)
        if status == "done":
            done += 1
        elif status == "failed":
            failed += 1
        else:
            # jpg/png 以外。投函口は一覧が無いので対象外も件数で知らせる。
            skipped += 1

    result = {
        "accepted": done + failed,
        "done": done,
        "failed": failed,
        "skipped": skipped,
    }
    return _drop_page(request, token, result=result)
