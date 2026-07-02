"""日乗（nichijou）— FastAPI アプリ本体。

画面は1枚（index.html）のみ。開いてすぐ書ける、を最優先にする。
編集ルートは存在しない（PUT/PATCH なし）。これは思想上の核。
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from PIL import Image

from . import db, vision

app = FastAPI(title="日乗 nichijou")

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
        ext = _ALLOWED_IMAGES.get(photo.content_type)
        if ext is None:
            # jpg/png 以外は黙って無視（摩擦を増やさない日乗の流儀）。
            continue
        data = await photo.read()
        filename = _save_photo(data, ext)
        taken_at = _read_taken_at(db.PHOTOS_DIR / filename)
        try:
            j = vision.classify(data, photo.content_type)
            db.add_observation(filename, j.species, j.confidence, j.comment, "done", taken_at)
        except Exception:
            # 判定に失敗しても写真は残す。後から再判定でやり直せる。
            db.add_observation(filename, None, None, None, "failed", taken_at)
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
