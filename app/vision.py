"""AI 判定層。

同じ被写体を写した写真（1枚以上）を Anthropic の vision モデルに1回で渡し、
種類・確信度・一言コメントを得る。判定ロジックはこのモジュールに閉じる
（db が SQLite を独占するのと同じ整理）。

複数枚渡せるのは、1枚では角度や解像度の都合で外すことがあるため。複数の
手がかりを突き合わせれば同定精度が上がる。コスト増は画像枚数分の入力トークン
だけで、API コール自体は観察1件につき1回のまま。

API キーは環境変数 ANTHROPIC_API_KEY から読む（コード・リポジトリには置かない）。
失敗（ネット断・タイムアウト等）は例外として呼び出し側へ伝え、そちらで
「判定失敗」状態にする。
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from datetime import datetime

import anthropic

# vision 対応の標準モデル（ご指定どおり sonnet 系）。
MODEL = "claude-sonnet-4-6"

# 構造化出力のスキーマ。「JSON で返して」と頼むより堅く、形を保証する。
_SCHEMA = {
    "type": "object",
    "properties": {
        "species": {"type": "string"},
        "confidence": {"type": "string", "enum": ["高", "中", "低"]},
        "comment": {"type": "string"},
    },
    "required": ["species", "confidence", "comment"],
    "additionalProperties": False,
}

# 複数枚を渡すときだけ先頭に添える前置き。「別々の被写体を並べた」のではなく
# 「同じ被写体を複数の角度・解像度で撮った」ものだと明示し、不鮮明な1枚に
# 引きずられて全体の判断を落とすことを防ぐ。
_MULTI_PREFIX = (
    "これから{n}枚の写真を示します。これらは同一の被写体を複数の角度・解像度で"
    "撮影したものです。別々の対象ではありません。全ての写真を総合して、1つの種として"
    "同定してください。解像度が高く特徴が明瞭に写っている写真を重視し、不鮮明な写真や"
    "写りの悪い角度の写真に引きずられないでください。写真ごとに別々の答えを出さず、"
    "最終的な同定は1つだけ返してください。\n\n"
)

_PROMPT = (
    "これは日本の川沿いで撮影された生き物または植物の写真です。"
    "種を可能な範囲で同定してください。種まで断定できない場合は、無理をせず"
    "分かる分類階級（属・科など）まででかまいません。"
    "確信度（高・中・低）と、日本語のコメントを添えて、指定の JSON スキーマに"
    "従って返してください。"
    "comment は1〜2文で、まず同定の補足（特徴・見分け方・生態など）を述べ、"
    "続けて観察者への短い一言を添えてください。散歩の相棒が静かに添えるような"
    "一言にし、大げさな感嘆や絵文字は使わないでください。"
    "場所や地名の推定はしないでください。"
)

# 撮影時期のヒント。植物などは花期・季節で候補が絞れる（例: ハルジオンとヒメジョオン）。
# 月は補助情報にとどめ、画像の特徴と矛盾する場合は画像を優先させる旨を明記する。
_SEASON_HINT = (
    "なお、この写真は{month}月に日本の川沿いで撮影されました。"
    "花期や季節が同定の手がかりになる場合は考慮してください。"
    "ただし時期だけで断定せず、画像の特徴と矛盾する場合は画像を優先してください。"
)

# 観察者が現場で添えたメモ。AI は写真しか見ていないが、観察者は実物を見ており、
# 大きさ・動き・鳴き声など写真に写らない情報を持つ。この非対称を補うために渡す。
# ただし同定はあくまで画像の特徴に基づいて独立に行わせ、メモに迎合させない。
_NOTE_HINT = (
    "\n\n観察者が現場で次のメモを添えています: 「{note}」\n"
    "これは現場でしか得られない貴重な情報（大きさ・行動・鳴き声、心当たりの種など）"
    "として考慮してください。ただし同定は画像の特徴に基づいて独立に行ってください。"
    "メモの推測と画像の特徴が矛盾する場合は画像を優先し、その旨を comment で丁寧に"
    "述べてください（例: メモでは◯◯とのことですが、画像の△△からは□□と思われます）。"
    "メモに迎合しないでください。"
)

# 過去の観察を踏まえた「観察者への一言」を促すためのヒント。履歴があるときだけ添える。
# 同種の再登場・今年初・頻度の変化など、本当に繋がりがある場合のみ触れさせ、無理に
# 過去へ繋げないよう明示的に抑制する（散歩の相棒の、静かな一言に留める）。
_HISTORY_HINT = (
    "\n\nこの写真の撮影日は{date}です。"
    "参考として、これまでの観察の記録を新しい順に挙げます"
    "（日付と種名のみ。写真や本文は渡していません）:\n{lines}\n"
    "同定した種がこの履歴に現れていて、本当に繋がりがある場合"
    "（同じ種の再登場、今年に入って初めて、その月内での再会、出現頻度の変化など）"
    "のみ、comment の一言でそれに触れてください。日付や回数に触れるときは、"
    "上の履歴から読み取れる範囲だけにしてください。"
    "履歴に無い種や、はっきりした繋がりが無いときは、無理に過去へ繋げず、"
    "季節や種についての素直な一言でかまいません。"
)


def _format_history(history: list[tuple[datetime, str]]) -> str:
    """履歴を「YYYY-MM-DD 種名」の短い行の並びに整形する（新しい順のまま）。"""
    return "\n".join(
        f"{taken_at.astimezone():%Y-%m-%d} {species}" for taken_at, species in history
    )


def _build_prompt(
    taken_at: datetime | None,
    history: list[tuple[datetime, str]] | None,
    observer_note: str | None = None,
    image_count: int = 1,
) -> str:
    """基本プロンプトに、複数枚の前置き・撮影月のヒント・観察履歴・観察者メモを
    （あれば）添えて返す。1枚のときは従来と同一のプロンプトになる。"""
    prompt = (_MULTI_PREFIX.format(n=image_count) if image_count > 1 else "") + _PROMPT
    if taken_at is not None:
        prompt += _SEASON_HINT.format(month=taken_at.astimezone().month)
    if history:
        # 撮影日が無い写真でも履歴照合はできるよう、日付は taken_at 依存にしない。
        date = f"{taken_at.astimezone():%Y-%m-%d}" if taken_at is not None else "不明"
        prompt += _HISTORY_HINT.format(date=date, lines=_format_history(history))
    # 観察者メモは、あるときだけ添える（無ければ従来と同一のプロンプト）。
    if observer_note and observer_note.strip():
        prompt += _NOTE_HINT.format(note=observer_note.strip())
    return prompt


@dataclass(frozen=True)
class Judgement:
    species: str
    confidence: str
    comment: str


def classify(
    images: list[tuple[bytes, str]],
    taken_at: datetime | None = None,
    history: list[tuple[datetime, str]] | None = None,
    observer_note: str | None = None,
) -> Judgement:
    """同じ被写体の写真（1枚以上）をまとめて判定する。images は
    (画像バイト列, media_type) の並び。失敗時は例外を送出する（呼び出し側で捕捉）。

    複数枚あるときは1つの user メッセージに複数の image ブロックとして並べ、
    総合して1種を同定させる（API コールは1回）。1枚のときは従来と同じ挙動。

    taken_at（撮影日時）を渡すと、その月を同定の補助手がかりとしてプロンプトに
    添える。history（直近の観察の日付＋種名。db 側で取得して渡す）を渡すと、
    同定した種が履歴にあれば、それを踏まえた一言を comment に含めさせる。
    observer_note（観察者が現場で添えたメモ）を渡すと、写真に写らない現場情報
    として考慮させる（同定は画像に基づき独立に行わせ、メモには迎合させない）。
    いずれも無ければ従来どおり画像のみで判定する（1 コールで完結。2 段階にしない）。"""
    if not images:
        raise ValueError("判定には写真が最低1枚必要")
    client = anthropic.Anthropic()
    prompt = _build_prompt(taken_at, history, observer_note, image_count=len(images))
    # 画像を先に並べ、最後に指示文を置く（複数画像でも指示が末尾で効くように）。
    content: list[dict] = []
    for image_bytes, media_type in images:
        content.append(
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": media_type,
                    "data": base64.standard_b64encode(image_bytes).decode("utf-8"),
                },
            }
        )
    content.append({"type": "text", "text": prompt})
    response = client.messages.create(
        model=MODEL,
        max_tokens=1024,
        messages=[{"role": "user", "content": content}],
        output_config={"format": {"type": "json_schema", "schema": _SCHEMA}},
    )
    # 構造化出力なので先頭の text ブロックは有効な JSON。
    text = next(block.text for block in response.content if block.type == "text")
    parsed = json.loads(text)
    return Judgement(
        species=parsed["species"],
        confidence=parsed["confidence"],
        comment=parsed["comment"],
    )
