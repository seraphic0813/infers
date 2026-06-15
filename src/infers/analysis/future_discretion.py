"""未来裁量エンジン (設計書 §5 / マニュアル 4項)。

「チャートに描かれていない未来」を (時間 k × 価格 P) 平面上の逆問題として解く:

  S = { (P, k) : 到達時点で RSI極値・SMA接触・レジサポ・フィボ目標のうち
                 2 family 以上が合流する }

鍵となる2つの性質:
  1. SMAの未来値は「未来終値の合計」のみに依存 → 線形パス仮定で
     接触価格が閉形式解になる (設計書 §5.2):
       P*(k) = ( S_known(k) + c0·(k−1)/2 ) / ( m − (k+1)/2 )
  2. RSI はパス依存 → Wilder状態からの前進計算 (rsi_forward, 純粋関数) を
     4種の代表パス族で評価し、バンド [lo, hi] として扱う (設計書 §5.3)

計算規約: 価格は整数ティック、導出値は固定量子化 Decimal (CLAUDE.md 第6条)。
合流点は (価格×時間) の点であり純粋な価格の点ではないため、指値候補には
必ず失効時刻 (expiry) を付与し、毎確定足で再計算する (CLAUDE.md 手法ロジックの正)。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Sequence

from infers.analysis.indicators import Q, RsiState, rsi_forward
from infers.analysis.micro import RSI_OVERBOUGHT, RSI_OVERSOLD
from infers.analysis.support_resistance import SRZone

# 戻り挟みパスの既定反発比率 (設計書 §5.3 パス族表)
RETRACE_RATIO = Decimal("0.3")


# ---------------------------------------------------------------------------
# §5.2 SMA前方投影 — 「伸びてくるSMAとの接触点」の閉形式解
# ---------------------------------------------------------------------------

def _s_known(closes: Sequence[int], period: int, k: int) -> int:
    """S_known(k) = 直近 (m−k) 本の既知終値の和 (末尾 = c0 を含む)。"""
    if not 1 <= k < period:
        raise ValueError(f"k must satisfy 1 <= k < period, got k={k}, period={period}")
    if len(closes) < period:
        raise ValueError(f"need at least {period} known closes, got {len(closes)}")
    return sum(closes[-(period - k):])


def sma_forward_linear(closes: Sequence[int], period: int, k: int, target_int: int) -> Decimal:
    """k本後のSMA値。c0 → target への線形パスを仮定 (Σc_i が閉形式)。

    Σ_{i=1..k} c_i = k·c0 + (target − c0)·(k+1)/2
    """
    c0 = closes[-1]
    s_known = _s_known(closes, period, k)
    sum_future = Decimal(k) * c0 + Decimal(target_int - c0) * (k + 1) / 2
    return ((s_known + sum_future) / period).quantize(Q)


def sma_touch_price(closes: Sequence[int], period: int, k: int) -> Decimal:
    """接触条件 P = SMA(t0+k) の閉形式解 (設計書 §5.2 検証済み導出式)。

      P*(k) = ( S_known(k) + c0·(k−1)/2 ) / ( m − (k+1)/2 )

    k = 1..K を掃引すると「SMAタッチ曲線」が得られる。これがマニュアル4の
    『時間経過によって伸びてきたSMAにピタリと接触する価格』の数学的実体。
    """
    c0 = closes[-1]
    s_known = _s_known(closes, period, k)
    numerator = Decimal(s_known) + Decimal(c0) * (k - 1) / 2
    denominator = Decimal(period) - Decimal(k + 1) / 2
    return (numerator / denominator).quantize(Q)


def sma_touch_curve(closes: Sequence[int], period: int, horizon: int) -> dict[int, Decimal]:
    """k = 1..min(horizon, period−1) のSMAタッチ曲線 P*(k)。"""
    k_max = min(horizon, period - 1)
    return {k: sma_touch_price(closes, period, k) for k in range(1, k_max + 1)}


def sma_slope_sign(closes: Sequence[int], period: int, lookback: int) -> int:
    """確定終値から算出した SMA(period) の実現傾き符号 (+1上昇/0横這い/−1下降)。

    グランビル判定(買②③は「傾きが逆行していない」)の傾き整合チェックに使う。
    末尾 period 本の和と、lookback 本だけ過去の同期間和を比較する(同じ本数なので
    和の大小がそのまま傾きの符号)。履歴が足りなければ 0(横這い扱い=逆行しない)。
    """
    n = len(closes)
    if lookback < 1 or n < period + lookback:
        return 0
    now = sum(closes[n - period:])
    prev = sum(closes[n - period - lookback:n - lookback])
    return (now > prev) - (now < prev)


# ---------------------------------------------------------------------------
# §5.3 RSI逆算 — 4種の代表パス族によるバンド評価
# ---------------------------------------------------------------------------

def _round_int(x: Decimal) -> int:
    return int(x.to_integral_value(rounding=ROUND_HALF_EVEN))


def _interp_path(c0: int, k: int, anchors: list[tuple[int, int]]) -> list[int]:
    """アンカー点 (バー番号, 価格) を線形補間した整数ティックの終値列 (長さk)。"""
    path: list[int] = []
    prev_j, prev_p = 0, c0
    for j, p in anchors:
        for t in range(prev_j + 1, j + 1):
            interp = Decimal(prev_p) + Decimal(p - prev_p) * (t - prev_j) / (j - prev_j)
            path.append(_round_int(interp))
        path[j - 1] = p  # アンカー値は厳密一致させる
        prev_j, prev_p = j, p
    return path


def make_paths(c0: int, target_int: int, k: int,
               retrace_ratio: Decimal = RETRACE_RATIO) -> dict[str, list[int]]:
    """設計書 §5.3 の代表パス族。すべて k 本で target に厳密到達する。

    - linear      : 等分 (中央推定値)
    - front_loaded: 序盤に変動集中 → 平滑減衰で到達時の極値が緩む
    - back_loaded : 直近バーに変動集中 → 到達時に最も極値が出やすい
    - retrace     : 途中に反発を挟む → 逆方向の gain/loss が混入し最も緩い
    """
    move = target_int - c0
    paths: dict[str, list[int]] = {
        "linear": _interp_path(c0, k, [(k, target_int)]),
        "front_loaded": _interp_path(c0, k, [(1, target_int), (k, target_int)]),
        "back_loaded": (
            _interp_path(c0, k, [(k - 1, c0), (k, target_int)]) if k >= 2
            else _interp_path(c0, k, [(k, target_int)])
        ),
    }
    if k >= 3:
        j1 = max(1, k // 3)
        j2 = max(j1 + 1, (2 * k) // 3)
        if j2 < k:
            q1 = c0 + _round_int(Decimal(move) * 2 / 3)
            q2 = q1 - _round_int(retrace_ratio * move)   # 反発 (進行方向と逆)
            paths["retrace"] = _interp_path(c0, k, [(j1, q1), (j2, q2), (k, target_int)])
    if "retrace" not in paths:
        paths["retrace"] = list(paths["linear"])
    return paths


@dataclass(frozen=True)
class RsiBand:
    """価格 target に k 本で到達したと仮定した場合の到達時RSIバンド。"""

    target_int: int
    k: int
    lo: Decimal
    hi: Decimal
    by_path: dict[str, Decimal]

    @property
    def oversold_certain(self) -> bool:
        """全パスで RSI<=30 (ほぼ確実に売られすぎ到達)。"""
        return self.hi <= RSI_OVERSOLD

    @property
    def oversold_possible(self) -> bool:
        """少なくとも一部のパスで RSI<=30 (パス次第)。相反veto判定に使う(安全側)。"""
        return self.lo <= RSI_OVERSOLD

    @property
    def oversold_likely(self) -> bool:
        """中心パス(linear)で RSI<=30 に到達。M5加点判定はこちらを使う(厳格化)。

        `oversold_possible` (全パス中の最良ケースが到達=hi/loの片端) は、十分離れた
        target では「到達可能性あり」がほぼ常に成立してしまい判別力を失う。中心的な
        パス1本での到達を要求することで、M5 RSI family が常時成立する問題を防ぐ。
        """
        return self.by_path["linear"] <= RSI_OVERSOLD

    @property
    def overbought_certain(self) -> bool:
        return self.lo >= RSI_OVERBOUGHT

    @property
    def overbought_possible(self) -> bool:
        """少なくとも一部のパスで RSI>=70 (パス次第)。相反veto判定に使う(安全側)。"""
        return self.hi >= RSI_OVERBOUGHT

    @property
    def overbought_likely(self) -> bool:
        """中心パス(linear)で RSI>=70 に到達。M5加点判定はこちらを使う(厳格化)。"""
        return self.by_path["linear"] >= RSI_OVERBOUGHT


def rsi_band(state: RsiState, target_int: int, k: int,
             retrace_ratio: Decimal = RETRACE_RATIO) -> RsiBand:
    """Wilder状態 state から (target, k) 到達時のRSIをパス族でバンド評価する。

    rsi_forward (純粋関数) を用いるため state は変更されない。
    実測更新と完全に同一の計算経路 (indicators.py 参照)。
    """
    if k < 1:
        raise ValueError("k must be >= 1")
    results = {
        name: rsi_forward(state, path)[0]
        for name, path in make_paths(state.last_close_int, target_int, k, retrace_ratio).items()
    }
    return RsiBand(
        target_int=target_int, k=k,
        lo=min(results.values()), hi=max(results.values()),
        by_path=results,
    )


# ---------------------------------------------------------------------------
# §5.4 未来コンフルエンスマップ — (k, P) グリッド合成
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class FutureCell:
    """グリッド上の1セル。families >= 2 かつ非veto のセルのみマップに残る。"""

    k: int
    price_int: int
    families: tuple[str, ...]      # 例 ("RSI", "SR") — family単位 (重複なし)
    score: Decimal                 # 1 family = 1 点 (二値加点。手法G2 ①)
    rsi: RsiBand
    rsi_strength: str = "NONE"     # RSI根拠の強度 HIGH/MEDIUM/LOW/NONE (手法G2-⑤③)
    sr_strength: Decimal = Decimal(0)  # 接触サポートゾーンの減衰加重強度 (手法G2-⑥④)
    sma_strength: str = "NONE"     # SMAグランビル根拠の強度 HIGH/MEDIUM/NONE (手法G2-⑦④)
    dow_strength: str = "NONE"     # ダウ順行根拠の強度 HIGH/MEDIUM/NONE (手法G2-⑧③)


def build_future_map(
    *,
    closes: Sequence[int],                 # 既知終値 (古→新、末尾=c0)
    rsi_state: RsiState,
    direction: int,                        # +1 買い(下方到達点) / -1 売り(上方)
    k_range: range,
    prices: Sequence[int],
    sma_periods: Sequence[int] = (90, 200),
    sr_zones: Sequence[SRZone] = (),
    fib_levels: Sequence[int] = (),
    sma_tol_ticks: int = 1,                # 呼び出し側がATRから算出 (例 0.3×ATR)
    fib_tol_ticks: int = 1,
    min_families: int = 2,
    score_fib: bool = True,                # False: FIBをコンフルエンス・スコアから除外
    htf_rsi_aligned: int = 0,              # 上位足(H1/D1)RSIが順方向にトリガーしたTF数 (G2-⑤③)
    htf_rsi_conflict: int = 0,             # 上位足(H1/D1)RSIが逆方向にトリガーしたTF数 (相反: G2-⑤③)
    sma_far_ticks: int = 10**9,            # 買④/売④の「極端乖離」閾値 (θ_far×ATR)。既定は実質OFF
    sma_slope_lookback: int = 5,           # SMA傾き符号の算出ルックバック (買②③の整合チェック)
    dow_aligned: bool = False,             # ミクロダウが順方向/反転初動 → "DOW" family +1 (G2-⑧③)
    dow_conflict: bool = False,            # ミクロダウが明確な逆行 → クラスタ破壊 (相反: G2-⑧③)
    dow_strength: str = "NONE",            # ダウ順行根拠の強度 HIGH(順行)/MEDIUM(反転初動)/NONE
) -> list[FutureCell]:
    """(k, P) グリッドを評価し、根拠 >=2 family の非veto セルだけを返す (純粋関数)。

    スコアは「1 family = 1 点」(手法G2 ①。複雑な重み付けはしない)。family は確定済み
    コンフルエンス (confluence.py) と同じ思想で数える: 複数SMA期間のヒットも "SMA" 1
    family。マニュアル3.4 の絶対条件 (根拠2つ以上) を未来時点にもそのまま適用する。

    **クラスタ破壊 (veto / NO-TRADE)**: 方向に相反する根拠が現れたセルは、たとえ他の
    family が2つ以上あっても破棄する (手法G2-⑤③/⑥③ の安全装置)。
      - RSI 相反: 上位足が逆方向の極値 (買い狙いで H1/D1 が ≥70 等)、または M5 前方
        バンドが逆方向の極値に達しうる場合。
      - SR 相反: 到達価格が逆行を阻む側のゾーン (買い狙いで上値のレジスタンス) に接触。

    RSI はマルチTF (手法G2-⑤): M5 は (k,P) 到達時の前方バンド (順方向の加点は
    oversold/overbought_likely = 中心パス(linear)基準。相反vetoは possible = 安全側の
    端点基準のまま) で、上位足(H1/D1)は突入イベント窓内のTF数を `htf_rsi_aligned`
    (順方向)/`htf_rsi_conflict`(逆方向) で受け取る (判定は呼び出し側の RsiExtremeRecency)。
    順方向のいずれかが立てば "RSI" family は **1点** 成立、強度は HIGH(上位足+M5)/
    MEDIUM(上位足のみ)/LOW(M5のみ) として記録する (点数は重なっても1点: ①)。

    SMA はグランビル (手法G2-⑦): M5前方投影SMAで、買②③ = SMAタッチ + 傾き順行
    (傾きが逆行していない) → 強度 HIGH、買④ = 極端な下方乖離 (≥ `sma_far_ticks`) →
    強度 MEDIUM。複数期間(90/200)が同時にヒットすれば密集として強度 HIGH。点数は
    重なっても1点 (①)。傾き逆行のタッチは根拠にしない。上位足の下向きSMAが上値を
    覆う「壁」のクラスタ破壊 (④マトリクスの相反行) は上位足SMA整備フェーズで対応。

    ダウ順行 (手法G2-⑧): ミクロのダウ状態は (k,P) に依存しない定数なので、呼び出し側
    (provider) が方向に対して分類 (classify_dow) し、`dow_aligned`(順行/反転初動→"DOW"
    family +1)・`dow_conflict`(明確な逆行→全セル破壊)・`dow_strength`(HIGH/MEDIUM) で渡す。
    """
    if direction not in (+1, -1):
        raise ValueError("direction must be +1 or -1")
    if min_families < 2:
        raise ValueError("min_families must be >= 2 (CLAUDE.md rule 5)")
    if dow_conflict:
        return []                          # ミクロが明確な逆行 → NO-TRADE (落ちるナイフ)

    cells: list[FutureCell] = []
    for k in k_range:
        for p in prices:
            families: list[str] = []
            score = Decimal(0)

            # --- ダウ順行 (手法G2-⑧③): 順行/反転初動なら1点 (相反は冒頭で全破棄済み) ---
            if dow_aligned:
                families.append("DOW")
                score += 1

            # --- RSIマルチTF (手法G2-⑤②③): 相反→veto、順方向→1点+強度 ---
            band = rsi_band(rsi_state, p, k)
            if direction > 0:
                m5_aligned, m5_conflict = band.oversold_likely, band.overbought_possible
            else:
                m5_aligned, m5_conflict = band.overbought_likely, band.oversold_possible
            # 相反状態 (方向の不一致) は直ちにコンフルエンス不成立 = クラスタ破壊
            # (m5_conflict は安全側の possible 判定を維持: 加点条件のみ厳格化する)
            if htf_rsi_conflict > 0 or m5_conflict:
                continue
            rsi_strength = "NONE"
            if htf_rsi_aligned > 0 or m5_aligned:
                families.append("RSI")
                score += 1
                rsi_strength = (
                    "HIGH" if (htf_rsi_aligned > 0 and m5_aligned)
                    else "MEDIUM" if htf_rsi_aligned > 0
                    else "LOW"
                )

            # --- SMA / グランビル (手法G2-⑦): M5前方投影SMAで判定。期間が複数
            #     ヒットしても "SMA" 1 family (重なりは強度に反映)。 ---
            #       買②③: SMAタッチ + 傾き順行 (傾きが逆行していない) → 強度 HIGH
            #       買④  : 価格がSMAから極端に下方乖離 (≥ far) → 強度 MEDIUM (要他根拠)
            #     傾き逆行のタッチ(下向きSMAへの接触=抵抗)は根拠にしない。上位足の
            #     下向きSMAが上値を覆う「壁」のクラスタ破壊は上位足SMA整備フェーズで対応。
            sma_strength = "NONE"
            sma_touch = False
            sma_dev = False
            sma_hits = 0
            for period in sma_periods:
                if k >= period or len(closes) < period:
                    continue
                proj = sma_forward_linear(closes, period, k, p)
                d = proj - Decimal(p)              # >0: SMAが価格より上 (買い側の下方乖離)
                slope = sma_slope_sign(closes, period, sma_slope_lookback)
                aligned = slope >= 0 if direction > 0 else slope <= 0
                if abs(d) <= sma_tol_ticks:
                    if aligned:                    # 買②③ (傾き順行のSMAサポートタッチ)
                        sma_touch = True
                        sma_hits += 1
                elif (d >= sma_far_ticks if direction > 0 else -d >= sma_far_ticks):
                    sma_dev = True                 # 買④/売④ (極端乖離からの回帰)
                    sma_hits += 1
            if sma_touch or sma_dev:
                families.append("SMA")
                score += 1
                # 買②③ タッチ or 複数SMA密集 → 高、買④ 単独 → 中 (手法G2-⑦④)
                sma_strength = "HIGH" if (sma_touch or sma_hits >= 2) else "MEDIUM"

            # --- 水平レジサポ (手法G2-⑥②③): ゾーンの役割 (現在価格基準で算出済み、
            #     ブレイク役割転換は build_zones/SRZoneTracker の責務) で判定する。
            #     買いは順行を支える SUPPORT 接触で +1、逆行を阻む RESISTANCE 接触は
            #     クラスタ破壊。売りは対称。複数ラインが接触しても family は1つ。 ---
            aligned_role = "SUPPORT" if direction > 0 else "RESISTANCE"
            sr_strength = Decimal(0)
            sr_opposed = False
            for z in sr_zones:
                if not z.contains(p):
                    continue
                if z.role == aligned_role:
                    sr_strength = max(sr_strength, z.strength)
                else:
                    sr_opposed = True
            if sr_opposed:
                continue                       # 逆行を阻む帯に接触 → クラスタ破壊
            if sr_strength > 0:
                families.append("SR")
                score += 1

            # --- フィボナッチ目標 (押し目ゾーン文脈)。score_fib=False で中核根拠の
            #     水増しを防ぐ: FIB単独では family数を満たさず弱い設定が足切りされる ---
            if score_fib and any(abs(p - lvl) <= fib_tol_ticks for lvl in fib_levels):
                families.append("FIB")
                score += 1

            if len(families) >= min_families:
                cells.append(FutureCell(
                    k=k, price_int=p,
                    families=tuple(families), score=score.quantize(Q), rsi=band,
                    rsi_strength=rsi_strength, sr_strength=sr_strength.quantize(Q),
                    sma_strength=sma_strength, dow_strength=dow_strength,
                ))
    return cells


# ---------------------------------------------------------------------------
# §5.5 指値注文候補の生成 (失効管理つき)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class FutureConfluence:
    """未来コンフルエンス由来の指値候補 (常に打診サイズ専用)。

    合流点は (価格×時間) の点であり、SMAは時間とともに動く。
    そのため expiry (失効時刻) を必ず持ち、毎確定足で再計算して
    許容ドリフトを超えたら修正(amend)または取消す (設計書 §5.5)。
    invalidation_price (エリオット無効化) 抵触時は即キャンセル。
    """

    direction: int
    limit_price_int: int
    eta_window: tuple[int, int]            # 有効な k 範囲 [k_min, k_max]
    score: Decimal
    expiry: datetime                       # now + k_max × バー長
    families: tuple[str, ...]
    rsi_band: tuple[Decimal, Decimal]      # (lo, hi) — L2へ渡す期待値特徴量
    rsi_strength: str = "NONE"             # RSI根拠の強度 (採用セル基準。手法G2-⑤③)
    sr_strength: Decimal = Decimal(0)      # 接触サポートゾーンの強度 (手法G2-⑥④)
    sma_strength: str = "NONE"             # SMAグランビル根拠の強度 (手法G2-⑦④)
    dow_strength: str = "NONE"             # ダウ順行根拠の強度 (手法G2-⑧③)
    invalidation_price: int | None = None


def propose_limit_orders(
    cells: Sequence[FutureCell],
    *,
    direction: int,
    now: datetime,
    bar_duration: timedelta,
    price_step: int,
    invalidation_price: int | None = None,
) -> list[FutureConfluence]:
    """マップの隣接セルを価格方向に連結し、指値候補へ集約する (純粋関数)。

    - 価格が price_step で連続するセル群 = 1 候補
    - limit_price は群内最高スコアセル (同点なら早い k) の価格
    - `eta_window` と `rsi_band` は **採用した limit 価格のセルのみ** から導く
      (群全体ではない)。指値は単一価格に置かれるため、その価格に到達する
      時間窓・到達時RSIだけが判断材料として意味を持つ:
        * eta_window = その価格で成立する k の [min, max]
          (群全体だと価格レンジ分だけ k がほぼ全域化し情報価値を失う — P5)
        * rsi_band   = その価格の全 k × 全パス族にわたる [min lo, max hi]
          (best セル1個=最小k だけだと 4 パスが縮退して点になる — P4)
    - expiry = now + k_max × bar_duration (時間依存の失効。k_max も採用価格基準)
    """
    if not cells:
        return []

    by_price = sorted(cells, key=lambda c: (c.price_int, c.k))
    groups: list[list[FutureCell]] = [[by_price[0]]]
    for cell in by_price[1:]:
        if cell.price_int - groups[-1][-1].price_int <= price_step:
            groups[-1].append(cell)
        else:
            groups.append([cell])

    candidates: list[FutureConfluence] = []
    for group in groups:
        best = max(group, key=lambda c: (c.score, -c.k))
        at_price = [c for c in group if c.price_int == best.price_int]
        k_min = min(c.k for c in at_price)
        k_max = max(c.k for c in at_price)
        lo = min(c.rsi.lo for c in at_price)
        hi = max(c.rsi.hi for c in at_price)
        candidates.append(FutureConfluence(
            direction=direction,
            limit_price_int=best.price_int,
            eta_window=(k_min, k_max),
            score=best.score,
            expiry=now + k_max * bar_duration,
            families=best.families,
            rsi_band=(lo, hi),
            rsi_strength=best.rsi_strength,
            sr_strength=best.sr_strength,
            sma_strength=best.sma_strength,
            dow_strength=best.dow_strength,
            invalidation_price=invalidation_price,
        ))
    candidates.sort(key=lambda c: c.score, reverse=True)
    return candidates
