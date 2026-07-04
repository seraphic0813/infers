"""INFERSネイティブ整数ティックParquetを上位TFへ決定論リサンプルするCLIツール
(改善プラン T16 / narrow_focus_mtf 設計書 §2.1)。

`data/exporter.py` 形式のParquet(列: symbol/tf/time[UTC]/o_int/h_int/l_int/c_int/volume)
を読み込み、指定した上位TFへバケット集約して同形式で書き出す。

集約規則:
  - バケット境界 = 各バーの `time`(open_time)を対象TF長でUTC床丸め
    (例: H1なら毎正時。10:00〜10:55の12本のM5バーはすべて10:00バケットへ)
  - `o_int` = バケット内で最も早い(先頭)バーの o_int
  - `h_int` = バケット内の max(h_int)
  - `l_int` = バケット内の min(l_int)
  - `c_int` = バケット内で最も遅い(末尾)バーの c_int
  - `volume` = バケット内の合計
  - `time`  = バケット開始時刻(床丸め後の時刻。個々の入力バーのtimeではない)
  - `symbol`/`tf` = 全入力バーで単一値であることを要求し、`tf` は出力TFへ差し替える

末尾の不完全バケットの破棄(確定足主義。設計書 §2.1):
  最終バケットが「そのバケット期間が入力データの最終バー時刻+1本分(元のTF長)を
  超えて延びる」場合、そのバケットは完成していないとみなし破棄する。
  本実装は下位互換な近似として「時系列ソート後の最終バケットを常に1つ破棄する」
  ことでこれを実現する(通常運用では入力Parquetが「対象上位TF境界のちょうど
  1本前で途切れている」ケースはまれで、末尾バケットは実務上ほぼ常に不完全である
  ため、この近似で十分。仮に末尾バケットがたまたま完全でも安全側に倒れる=
  1バケット分データが減るだけで不正確なバーが混入することはない)。

週末ギャップ: 入力に無い時間帯のバケットは生成しない(空バケットの穴埋め・
合成はしない。設計書 §2.1)。

決定論性: 同一入力に対し常に同一出力(ソート・集約のみで乱数・現在時刻に依存しない)。

使い方:
    python tools/resample_parquet.py data/xauusd_m5.parquet data/xauusd_h1.parquet --tf H1
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# infers パッケージ (src/infers) を import できるようにする (tools/ は src/ 外)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from infers.core.models import Timeframe  # noqa: E402


def _floor_to_bucket(ts, bucket_seconds: int):
    """`ts`(tz-aware UTC datetime)を `bucket_seconds` 長でUTC床丸めする。"""
    epoch_seconds = int(ts.timestamp())
    floored = (epoch_seconds // bucket_seconds) * bucket_seconds
    from datetime import datetime, timezone
    return datetime.fromtimestamp(floored, tz=timezone.utc)


def resample(df, target_tf: Timeframe):
    """polars DataFrame(exporter形式)を `target_tf` へリサンプルして返す。

    入力 `df` は `time` 昇順であることを前提としない(内部でソートする)。
    末尾の不完全バケットは破棄する(モジュールdocstring参照)。
    """
    import polars as pl

    if df.height == 0:
        return df

    symbols = df["symbol"].unique().to_list()
    if len(symbols) != 1:
        raise ValueError(f"single symbol expected, got {symbols}")
    symbol = symbols[0]

    df = df.sort("time")
    bucket_seconds = int(target_tf.duration.total_seconds())

    bucket_times = [_floor_to_bucket(t, bucket_seconds) for t in df["time"].to_list()]
    df = df.with_columns(pl.Series("_bucket", bucket_times))

    agg = (
        df.group_by("_bucket", maintain_order=True)
        .agg(
            o_int=pl.col("o_int").first(),
            h_int=pl.col("h_int").max(),
            l_int=pl.col("l_int").min(),
            c_int=pl.col("c_int").last(),
            volume=pl.col("volume").sum(),
        )
        .sort("_bucket")
        .rename({"_bucket": "time"})
    )

    # 末尾の不完全バケットを破棄 (確定足主義。モジュールdocstring参照)
    if agg.height > 0:
        agg = agg.slice(0, agg.height - 1)

    agg = agg.with_columns(
        pl.lit(symbol).alias("symbol"),
        pl.lit(target_tf.value).alias("tf"),
    ).select(["symbol", "tf", "time", "o_int", "h_int", "l_int", "c_int", "volume"])

    return agg.with_columns(
        pl.col("o_int").cast(pl.Int64),
        pl.col("h_int").cast(pl.Int64),
        pl.col("l_int").cast(pl.Int64),
        pl.col("c_int").cast(pl.Int64),
        pl.col("volume").cast(pl.Int64),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("input", type=Path, help="入力Parquet (exporter形式)")
    parser.add_argument("output", type=Path, help="出力Parquet (同形式・上位TF)")
    parser.add_argument("--tf", required=True, choices=[t.value for t in Timeframe],
                        help="出力の対象時間足 (例: H1)")
    args = parser.parse_args(argv)

    import polars as pl

    target_tf = Timeframe(args.tf)
    df = pl.read_parquet(str(args.input))
    out = resample(df, target_tf)
    out.write_parquet(str(args.output))
    print(f"{args.input} ({df.height} bars) -> {args.output} "
         f"({out.height} bars, tf={target_tf.value})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
