"""AI 判定層。

写真1枚を Anthropic の vision モデルに渡し、種類・確信度・一言コメントを得る。
判定ロジックはこのモジュールに閉じる（db が SQLite を独占するのと同じ整理）。

API キーは環境変数 ANTHROPIC_API_KEY から読む（コード・リポジトリには置かない）。
呼び出しは保存時に写真1枚あたり1回。失敗（ネット断・タイムアウト等）は例外として
呼び出し側へ伝え、そちらで「判定失敗」状態にする。
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass

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

_PROMPT = (
    "これは日本の川沿いで撮影された生き物または植物の写真です。"
    "種を可能な範囲で同定してください。種まで断定できない場合は、無理をせず"
    "分かる分類階級（属・科など）まででかまいません。"
    "確信度（高・中・低）と、特徴や補足を述べる短い一言コメントを添えて、"
    "日本語で、指定の JSON スキーマに従って返してください。"
)


@dataclass(frozen=True)
class Judgement:
    species: str
    confidence: str
    comment: str


def classify(image_bytes: bytes, media_type: str) -> Judgement:
    """写真を判定する。失敗時は例外を送出する（呼び出し側で捕捉する）。"""
    client = anthropic.Anthropic()
    data = base64.standard_b64encode(image_bytes).decode("utf-8")
    response = client.messages.create(
        model=MODEL,
        max_tokens=1024,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": media_type,
                            "data": data,
                        },
                    },
                    {"type": "text", "text": _PROMPT},
                ],
            }
        ],
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
