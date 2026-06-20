"""執行モデル抽象 (段階2.3 / docs/phase2-architecture.md §4.2)。

`TradingLoop` (L0) は手法ごとに異なる執行手順 (打診→追撃→半利→ランナー等) を
直接知らず、本モジュールの `ExecutionModel` 抽象メソッドのみを呼ぶ。これにより
手法ごとに執行ライフサイクルを差し替えられる (例: 成行+固定TP/SL)。

CLAUDE.md §0 の安全原則は全執行モデルに強制され続ける契約:
  - 防御はLLM非依存 — ExecutionModel は LLM を一切呼ばない (第1条)
  - 確定足主義 — on_bar は確定足のみ受領する (第2条)
  - SL単調性 — SL変更は実装内のガードを必ず経由する (第3条)
  - 冪等性 — 全注文操作に決定論的 client_order_id を付与する (第10条)
自由になるのは「いつ・どの価格で建て、どう手仕舞うか」の戦術だけ。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from infers.core.models import Candle


@dataclass(frozen=True)
class BarOutcome:
    """確定足1本を処理した結果として、TradingLoop が知る必要のある事実のみ。

    - closed: この足でポジションが終端 (CLOSED) に達したか。
    - expired: 打診指値が「時間切れ失効」で取り消されたか (シナリオ崩壊=無効化
      とは区別する。失効のみクールダウン即時解除の対象。entry-methodology.md ※例外)。
    """

    closed: bool = False
    expired: bool = False


class ExecutionModel(Protocol):
    """1ポジションのライフサイクルを管理する執行モデル (L0抽象)。

    TradingLoop が呼ぶのはこの4メソッド + 2プロパティのみ。手法固有の執行
    ロジック (建値SL移動・部分利確・追撃・残玉決済) は実装内部に隠蔽される。

    `intent` / `signal` を `object` 型に保つことで、L0 はいかなる手法固有の
    語彙 (TradePlan の w1_high_int 等、ProviderOutput の structure_events 等) も
    import せず、これらの解釈は完全に実装側 (L2) の責務となる。
    """

    @property
    def volume_steps(self) -> int:
        """現在保有量 (ロットステップ)。リスク層の建玉合計算定に用いる。"""
        ...

    @property
    def closed(self) -> bool:
        """終端 (CLOSED) に達したか。True ならループが open_positions から外す。"""
        ...

    def place(self, intent: object) -> None:
        """初期発注 (成行/指値)。`intent` は手法固有のエントリー意図。"""
        ...

    def on_broker_event(self, ev: object) -> None:
        """ブローカーイベント (約定/SLヒット) を受けて状態を進める。"""
        ...

    def on_bar(self, candle: Candle, signal: object) -> BarOutcome:
        """確定足1本の自己管理。`signal` は同手法の分析層が出した管理シグナル。"""
        ...

    def close(self, reason: str) -> None:
        """残存ポジションの手仕舞い (データ末尾・シャットダウン)。"""
        ...
