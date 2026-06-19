"""エリオット波動カウンター (設計書 §3.3 / CLAUDE.md「手法ロジックの正」)。

設計の核心:
  3つの絶対原則は「この価格を割ったらカウント無効」という
  【無効化価格 (invalidation_price)】に変換できる。これにより
  毎ティックの再検証が O(1) (整数比較1〜2回) になり、同じ価格を
  そのまま損切り・シナリオ破棄ラインとして執行層へ流用できる。

  - 原則② (第2波は第1波始点を割らない) → 波1〜3進行中: inv = P0
  - 原則③ (第4波は第1波高値を割らない) → 波4〜5進行中: inv = P1
  - 原則① (第3波は最短にならない)      → 第5波進行中かつ len3<len1 の
    場合のみ、上側キャップ max_wave5_price = P4 + len3 が発生する

カウントは本質的に多義的なため、単一の正解を持たず候補集合
(ElliottView: top-N + 曖昧度) として保持する。曖昧度が小さい
(候補が拮抗する) 局面が L2 (Fable 5) への裁定エスカレーション条件になる。

入力は ZigZagDetector が確定させた SwingPoint のみ (確定足主義の連鎖)。
本モジュールの計算はすべて int (ティック) と固定量子化 Decimal で行う。
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from decimal import Decimal

from infers.indicators import Q
from infers.strategies.narrow_focus.zigzag import SwingPoint

# 1カウントの最大ピボット数: P0..P5 (推進5波)
_MAX_PIVOTS = 6
# スコアの理想比率と許容幅 (フィボナッチ適合度。Decimal固定)
_IDEAL_W2_RETRACE = Decimal("0.618")   # 第2波押し: 38.2〜78.6% が典型
_IDEAL_W3_EXT = Decimal("1.618")       # 第3波伸長
_IDEAL_W4_RETRACE = Decimal("0.382")
_TOL_RETRACE = Decimal("0.5")
_TOL_EXT = Decimal("1.5")
_PIVOT_BONUS = Decimal("0.05")         # 構造が多く確定しているほど加点


@dataclass(frozen=True)
class WaveCount:
    """単一のカウント候補 (設計書 §3.3)。

    direction: +1 = 上昇推進波 (P0=LOW起点) / -1 = 下降推進波 (P0=HIGH起点)
    current_wave: 進行中の波番号。pivots が k 個なら波 k が進行中
                  (k=2 → 第2波進行中)。k=6 で推進5波完了 (complete=True)。
    """

    direction: int
    pivots: tuple[SwingPoint, ...]          # P0..Pk (2〜6個, 交互のスイング)
    current_wave: int
    complete: bool
    invalidation_price: int                 # 原則②③由来。割れたら候補消滅
    max_wave5_price: int | None             # 原則①由来の上側(下降なら下側)キャップ
    score: Decimal                          # フィボ適合度 0..1 + 構造ボーナス
    tf: object = field(repr=False, default=None)

    # -- O(1) 無効化チェック (CLAUDE.md「手法ロジックの正」) -------------------

    def is_invalidated(self, price_int: int) -> bool:
        """現在価格がこのカウントを無効化するか。整数比較のみ (O(1))。"""
        if self.direction > 0:
            if price_int < self.invalidation_price:
                return True
            if self.max_wave5_price is not None and price_int > self.max_wave5_price:
                return True
        else:
            if price_int > self.invalidation_price:
                return True
            if self.max_wave5_price is not None and price_int < self.max_wave5_price:
                return True
        return False

    def wave_len(self, wave_no: int) -> int:
        """波 n の値幅 (正のティック数)。n=1,3,5 (推進波)・2,4 (修正波)。"""
        if wave_no >= len(self.pivots):
            raise ValueError(f"wave {wave_no} not yet pivoted")
        return abs(self.pivots[wave_no].price_int - self.pivots[wave_no - 1].price_int)


@dataclass(frozen=True)
class ElliottView:
    """カウント候補集合 (スコア降順)。

    ambiguity = 1位と2位のスコア差。小さいほど解釈が拮抗しており、
    AI判断層 (設計書 §7.1) のエスカレーション指標となる。
    候補が1つ以下なら曖昧性なしとして 1 を返す。
    """

    candidates: tuple[WaveCount, ...]

    @property
    def best(self) -> WaveCount | None:
        return self.candidates[0] if self.candidates else None

    @property
    def ambiguity(self) -> Decimal:
        if len(self.candidates) < 2:
            return Decimal(1)
        return (self.candidates[0].score - self.candidates[1].score).quantize(Q)


# ---------------------------------------------------------------------------
# 候補の構築と3原則の検証 (純粋関数群)
# ---------------------------------------------------------------------------

def _build_candidate(pivots: tuple[SwingPoint, ...]) -> WaveCount | None:
    """連続スイング列 (P0 起点) から候補を構築。3原則違反は None (即無効)。"""
    k = len(pivots)
    if k < 2 or k > _MAX_PIVOTS:
        return None

    # 方向: P0 が LOW なら上昇推進波。ZigZagの交互性により各レグの向きは自動成立
    s = 1 if pivots[0].kind == "LOW" else -1

    def d(i: int, j: int) -> int:
        """符号正規化した価格差: 上昇方向に正。"""
        return s * (pivots[j].price_int - pivots[i].price_int)

    # --- 原則② : 第2波は第1波の始点を割り込まない (P2 が P0 を超えて修正しない)
    if k >= 3 and d(0, 2) < 0:
        return None

    # --- 推進波の前提: 第3波は第1波の終点を更新する
    if k >= 4 and d(1, 3) <= 0:
        return None

    # --- 原則③ : 第4波は第1波の高値(終点)を割り込まない
    if k >= 5 and d(1, 4) < 0:
        return None

    len1 = abs(pivots[1].price_int - pivots[0].price_int)
    len3 = abs(pivots[3].price_int - pivots[2].price_int) if k >= 4 else None
    len5 = abs(pivots[5].price_int - pivots[4].price_int) if k == 6 else None

    # --- 原則① : 第3波は推進波 (1,3,5) の中で最短にならない
    if len5 is not None and len3 is not None and len3 < len1 and len3 < len5:
        return None

    # --- 無効化価格 (原則②③の変換。設計書 §3.3 の表)
    if k <= 4:
        inv = pivots[0].price_int        # 波1〜3進行中: 原則② → P0
    else:
        inv = pivots[1].price_int        # 波4〜5進行中/完了: 原則③ → P1

    # --- 原則①由来のキャップ: 第5波進行中 (k=5) かつ len3 < len1 のとき、
    #     第5波が len3 を超えて伸びると第3波が最短になるため上限が立つ
    cap: int | None = None
    if k == 5 and len3 is not None and len3 < len1:
        cap = pivots[4].price_int + s * len3

    return WaveCount(
        direction=s,
        pivots=pivots,
        current_wave=min(k, 5),
        complete=(k == _MAX_PIVOTS),
        invalidation_price=inv,
        max_wave5_price=cap,
        score=_score(pivots, s),
        tf=pivots[0].tf,
    )


def _component(ratio: Decimal, ideal: Decimal, tol: Decimal) -> Decimal:
    """フィボ適合度の1成分: |ratio−ideal| が tol で線形減衰 (0..1)。"""
    diff = abs(ratio - ideal)
    if diff >= tol:
        return Decimal(0)
    return ((tol - diff) / tol).quantize(Q)


def _score(pivots: tuple[SwingPoint, ...], s: int) -> Decimal:
    """候補スコア: フィボ比率適合度の平均 + 確定構造ボーナス。"""
    k = len(pivots)
    p = [pv.price_int for pv in pivots]
    len1 = abs(p[1] - p[0])
    parts: list[Decimal] = []

    if k >= 3 and len1 > 0:
        retrace2 = (Decimal(abs(p[1] - p[2])) / len1).quantize(Q)
        parts.append(_component(retrace2, _IDEAL_W2_RETRACE, _TOL_RETRACE))
    if k >= 4 and len1 > 0:
        ext3 = (Decimal(abs(p[3] - p[2])) / len1).quantize(Q)
        parts.append(_component(ext3, _IDEAL_W3_EXT, _TOL_EXT))
    if k >= 5:
        len3 = abs(p[3] - p[2])
        if len3 > 0:
            retrace4 = (Decimal(abs(p[3] - p[4])) / len3).quantize(Q)
            parts.append(_component(retrace4, _IDEAL_W4_RETRACE, _TOL_RETRACE))

    base = (sum(parts) / len(parts)).quantize(Q) if parts else Decimal("0.5")
    return (base + _PIVOT_BONUS * (k - 2)).quantize(Q)


def count_waves(swings: tuple[SwingPoint, ...], top_n: int = 3) -> ElliottView:
    """純粋関数: 確定スイング列の各サフィックスを候補として列挙・検証する。

    候補は必ず最新スイングまでを含む (現在進行形のカウントのみを扱う)。
    3原則に1つでも違反したサフィックスは候補にならない (即無効=リセット)。
    """
    n = len(swings)
    candidates: list[WaveCount] = []
    for start in range(max(0, n - _MAX_PIVOTS), n - 1):
        wc = _build_candidate(tuple(swings[start:]))
        if wc is not None:
            candidates.append(wc)
    candidates.sort(key=lambda w: w.score, reverse=True)
    return ElliottView(candidates=tuple(candidates[:top_n]))


class ElliottCounter:
    """ステートフルラッパー: 確定スイングを蓄積し、都度 ElliottView を返す。

    再計算は毎回バッファ全体から行う (インクリメンタル更新による
    状態バグを避け、純粋関数 count_waves に判定を集約する)。
    """

    def __init__(self, max_swings: int = 12, top_n: int = 3) -> None:
        if max_swings < 2:
            raise ValueError("max_swings must be >= 2")
        self._buf: deque[SwingPoint] = deque(maxlen=max_swings)
        self._top_n = top_n

    @property
    def swings(self) -> tuple[SwingPoint, ...]:
        return tuple(self._buf)

    def on_swing(self, swing: SwingPoint) -> ElliottView:
        if self._buf and self._buf[-1].kind == swing.kind:
            raise ValueError("swings must alternate HIGH/LOW (ZigZag contract)")
        if self._buf and swing.confirmed_at <= self._buf[-1].confirmed_at:
            raise ValueError("swings must be confirmed in increasing order")
        self._buf.append(swing)
        return count_waves(tuple(self._buf), self._top_n)
