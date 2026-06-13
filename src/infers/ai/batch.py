"""Batch API 連携 (設計書 §7.4 — 2パスバックテストのパス1/パス2境界)。

パス1: 決定論スイープで収集した裁定イベントを Batch API リクエスト
        (JSONL, 50%オフ・24h以内完了) として書き出す。custom_id には
        VerdictCache のキャッシュキーをそのまま使う。
パス2: Batch 結果 (JSONL) を VerdictCache へ取り込む。以後のリプレイは
        キャッシュヒットのみで進み、外部APIを一切叩かない。

実際の送信/取得 (client.messages.batches.create / results) はライブ運用
スクリプト側の責務。本モジュールはファイル形式の生成と取込のみを担い、
ネットワークに依存しない (CIで完全テスト可能)。
"""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path
from typing import Iterable, Sequence

from infers.ai.gateway import MODELS, JudgementRequest, Verdict, VerdictCache, cache_key


def _verdict_output_format() -> dict:
    """structured outputs 用の Verdict スキーマ (output_config.format)。

    messages.parse(output_format=Verdict) が送るものと同じワイヤ形式。
    SDK の transform_schema (strict化) が利用可能なら適用する
    (anthropic 未インストールのCI環境では素のJSONスキーマで代替)。
    """
    schema = Verdict.model_json_schema()
    try:  # pragma: no cover — SDK内部APIのため存在しない環境を許容
        from anthropic.lib._parse._transform import transform_schema
        schema = transform_schema(schema)
    except Exception:  # noqa: BLE001
        pass
    # confidence (Decimal) の anyOf[number, string] は string 分岐に
    # 範囲/パターン制約が効かず、"NO_GO" 等の非数値文字列が構造化出力の
    # 検証を通過してしまう (実例: parse_failed)。number 分岐のみに絞り、
    # 型レベルで非数値出力を排除する。
    confidence = schema.get("properties", {}).get("confidence")
    if isinstance(confidence, dict) and "anyOf" in confidence:
        number_branch = next(
            (b for b in confidence["anyOf"] if b.get("type") == "number"), None)
        if number_branch is not None:
            schema["properties"]["confidence"] = number_branch
    return {"type": "json_schema", "schema": schema}


def build_batch_request(request: JudgementRequest, tier: str, system_prompt: str) -> dict:
    """Anthropic Batch API の1リクエスト要素を構築する。

    custom_id = cache_key(request, tier) — 結果の取込先が一意に定まる。
    Verdict スキーマを structured outputs で強制する (CLAUDE.md 第8条:
    LLM出力はスキーマ強制。これがないと結果が自由文になり取込めない)。
    """
    params: dict = {
        "model": MODELS[tier],
        "max_tokens": 2048,
        "system": [{
            "type": "text",
            "text": system_prompt,
            "cache_control": {"type": "ephemeral"},
        }],
        "messages": [{
            "role": "user",
            "content": json.dumps(
                {"kind": request.kind.value, "symbol": request.symbol,
                 "direction": request.direction, "features": request.features},
                sort_keys=True, default=str, ensure_ascii=False),
        }],
        "output_config": {"format": _verdict_output_format()},
    }
    if tier == "L2":
        params["thinking"] = {"type": "adaptive"}
        params["output_config"]["effort"] = "high"
    return {"custom_id": cache_key(request, tier), "params": params}


def write_batch_file(
    items: Sequence[tuple[JudgementRequest, str]],
    path: str | Path,
    *,
    system_prompt: str,
) -> int:
    """(request, tier) の列を JSONL へ書き出す。custom_id 重複は1件に統合。

    戻り値は書き出した件数。
    """
    seen: set[str] = set()
    lines: list[str] = []
    for request, tier in items:
        entry = build_batch_request(request, tier, system_prompt)
        if entry["custom_id"] in seen:
            continue
        seen.add(entry["custom_id"])
        lines.append(json.dumps(entry, sort_keys=True, ensure_ascii=False))
    Path(path).write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    return len(lines)


_JSON_OBJECT = re.compile(r"\{.*\}", re.DOTALL)


def _validate_verdict(text: str) -> Verdict | None:
    """JSONテキストを Verdict として検証する。

    structured outputs はキー構造と型を強制するが、配列長 (maxItems) は
    API側で強制されない。情報フィールドである reasons の超過のみ
    先頭3件へ切り詰めて再検証する (decision/confidence は不変)。
    """
    try:
        return Verdict.model_validate_json(text)
    except Exception:  # noqa: BLE001
        pass
    try:
        data = json.loads(text)
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(data, dict):
        return None
    reasons = data.get("reasons")
    if isinstance(reasons, list) and len(reasons) > 3:
        data = {**data, "reasons": reasons[:3]}
    try:
        return Verdict.model_validate(data)
    except Exception:  # noqa: BLE001
        return None


def _parse_verdict(text: str) -> Verdict | None:
    """succeeded テキストから Verdict を復元する。

    1次: 全文をそのままJSONとして検証 (structured outputs の正常経路)。
    2次: 自由文に埋め込まれたJSONオブジェクトを抽出して検証
         (structured outputs 未指定で実行された旧バッチの救済)。
    """
    verdict = _validate_verdict(text)
    if verdict is not None:
        return verdict
    m = _JSON_OBJECT.search(text)
    if m is None:
        return None
    return _validate_verdict(m.group(0))


def ingest_batch_results(lines: Iterable[str], cache: VerdictCache,
                         *, stats: Counter | None = None) -> int:
    """Batch 結果 (JSONL 行) を VerdictCache へ取り込む。

    - succeeded のみ取込。失敗/スキーマ不整合の行はスキップ
      (恒久判定として固定しない — ガードレールと同じ思想)
    - stats を渡すと結果種別の内訳 (succeeded/errored/parse_failed 等) を
      集計する — 「取込0件」が沈黙しないための可視化 (CLAUDE.md 第11条)
    - 戻り値は取り込んだ件数
    """
    if stats is None:
        stats = Counter()
    count = 0
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
            result = row["result"]
            kind = result["type"]
            stats[kind] += 1
            if kind != "succeeded":
                continue
            text = next(
                block["text"] for block in result["message"]["content"]
                if block["type"] == "text"
            )
        except Exception:                          # noqa: BLE001 — 1行の不良で全体を止めない
            stats["malformed_line"] += 1
            continue
        verdict = _parse_verdict(text)
        if verdict is None:
            stats["parse_failed"] += 1
            continue
        cache.put(row["custom_id"], verdict)
        count += 1
    stats["ingested"] = count
    return count


def ingest_batch_results_file(path: str | Path, cache: VerdictCache,
                              *, stats: Counter | None = None) -> int:
    return ingest_batch_results(
        Path(path).read_text(encoding="utf-8").splitlines(), cache, stats=stats)
