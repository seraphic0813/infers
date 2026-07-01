"""atr_trend_scalp 手法固有の上位足リサンプラ (L2 / spec.md §2)。

執行TF(M5)の確定足を上位足(M15)へ集約し、**確定した上位足のみ**を1本ずつ
返す(形成中バーは出さない = リペイント禁止。CLAUDE.md 第2条)。境界は UTC 時刻の
床関数(M15 → 毎時 0/15/30/45 分)。

narrow_focus/provider.py の `MacroResampler` と同型だが、他手法フォルダへの
L2→L2 依存を避けるため本手法専用に自前実装する(smc_bos が structure.py を
自前実装したのと同じ判断。spec.md §5)。集約バーの volume は 0(上位足は終値の
EMA50 にしか使わず出来高を必要としないため)。将来、複数手法が使うようになれば
L1 の汎用リサンプラへ吸い上げる余地がある(narrow_focus のビット一致を脅かさない
新規追加の形で)。
"""

from __future__ import annotations

from datetime import datetime, timezone

from infers.core.models import Candle, Timeframe


class TfResampler:
    """下位足(base_tf)の確定足を上位足(target_tf)へ集約する。

    push() に下位足を1本ずつ投入し、直前の上位足バケットが完成したら
    その確定足を返す(未完成なら None)。target_tf.duration は base_tf.duration の
    整数倍である前提(M5→M15 は 3 倍)。
    """

    def __init__(self, symbol: str, target_tf: Timeframe) -> None:
        self.symbol = symbol
        self.tf = target_tf
        self._dur = int(target_tf.duration.total_seconds())
        self._bucket: int | None = None
        self._start: datetime | None = None
        self._o = self._h = self._l = self._c = 0

    def push(self, candle: Candle) -> Candle | None:
        """下位足を1本投入。直前の上位足が確定したらそれを返す(なければ None)。"""
        b = (int(candle.open_time.timestamp()) // self._dur) * self._dur
        completed: Candle | None = None
        if self._bucket is None:
            self._open(b, candle)
        elif b != self._bucket:
            completed = self._emit()
            self._open(b, candle)
        else:
            if candle.h_int > self._h:
                self._h = candle.h_int
            if candle.l_int < self._l:
                self._l = candle.l_int
            self._c = candle.c_int
        return completed

    def _open(self, b: int, candle: Candle) -> None:
        self._bucket = b
        self._start = datetime.fromtimestamp(b, tz=timezone.utc)
        self._o, self._h, self._l, self._c = (
            candle.o_int, candle.h_int, candle.l_int, candle.c_int)

    def _emit(self) -> Candle:
        assert self._start is not None
        return Candle(symbol=self.symbol, tf=self.tf, open_time=self._start,
                      o_int=self._o, h_int=self._h, l_int=self._l, c_int=self._c,
                      volume=0, is_closed=True)
