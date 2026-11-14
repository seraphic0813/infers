# ライブ監視 起動手順(PC再起動後など)

narrow_focus・atr_trend_scalp の2手法を、別々のMT5デモ口座(別ターミナル
インストール)で同時にライブ監視するための手順。PCを再起動するとプロセスは
残らないため、毎回この手順で再起動する。

> **smc_bos はライブ凍結中(2026-07-03)**。`reports/smc_bos_full/` の年次分解で
> 事前宣言基準(全暦年PF≥0.9)を満たさないことが確定した(2022年PF0.801の赤字・
> 2026年が純益の約60%)ため、2口座目でのライブ稼働を停止している。
> `scripts\start_live.ps1`/`.bat` は smc_bos を起動しないよう既に変更済み。
> 詳細・再開条件は [strategies/smc_bos/spec.md](../src/infers/strategies/smc_bos/spec.md)
> 「ライブ凍結ステータス」節を参照。以下の手順3(smc_bos起動)は**凍結解除まで実施しない**。

**`scripts\start_live.bat`(ダブルクリック)/ `scripts\start_live.ps1` が
この手順を自動化している。** 通常はそちらを使えばよく、以下は内部で
何が起きているかの説明と、手動操作が必要な場合の手順。
停止は `scripts\stop_live.bat`(ダブルクリック)/ `scripts\stop_live.ps1` —
3手法のPythonプロセスと対応するMT5ターミナル3つをまとめて停止する。

## 前提

- narrow_focus → `Vantage Prime MT5 Terminal`(既定口座)
- smc_bos → `Vantage Trading MT5 Terminal2`(2口座目)。**ライブ凍結中(起動しない)**
- atr_trend_scalp → `Vantage Trading MT5 Terminal3`(3口座目)
- 3つのMT5ターミナルは別フォルダにインストール済み。`--terminal-path` を
  **必ず全てに明示指定**すること。省略すると `mt5.initialize()` が
  「既に起動中の別ターミナル」に相乗りしてしまい、複数手法が同じ口座に
  繋がってしまう事故が起きる(実際に発生した不具合)。

## 手順

1. 残存プロセスがないか確認(PC再起動直後は通常ゼロ件のはず):

   ```bash
   powershell -Command "Get-CimInstance Win32_Process | Where-Object { \$_.Name -like '*terminal64*' -or \$_.CommandLine -like '*infers.main*live*' } | Select-Object ProcessId,Name"
   ```

   何か残っていた場合は、PIDを指定して個別に停止する(`-Name` 指定の
   一括killは他の無関係なMT5インスタンスも巻き込むため使わない):

   ```bash
   powershell -Command "Stop-Process -Id <PID1>,<PID2> -Force"
   ```

2. narrow_focus を起動し、ターミナル起動完了を待つ:

   ```bash
   source .venv/Scripts/activate 2>/dev/null
   python -m infers.main --mode live --demo --strategy narrow_focus --symbol XAUUSD \
     --journal work/journal/narrow_focus_xauusd.jsonl \
     --terminal-path "C:\Program Files\Vantage Prime MT5 Terminal\terminal64.exe" \
     > work/narrow_focus_live.log 2>&1 &
   ```

   `work/narrow_focus_live.log` に `[warm-up] fed N historical bars to provider`
   が出るまで待つ(MT5ターミナル起動+ヒストリカル取得+ウォームアップ完了の合図)。

3. ~~smc_bos を起動する~~ → **ライブ凍結中のためスキップ**(2026-07-03。上記の注記・
   spec.md 参照)。凍結解除が決定した場合のみ、以下のコマンドで起動する
   (`start_live.ps1` の対応ブロックも合わせて復元すること):

   ```bash
   python -m infers.main --mode live --demo --strategy smc_bos --symbol XAUUSD \
     --journal work/journal/smc_bos_xauusd.jsonl \
     --terminal-path "C:\Program Files\Vantage Trading MT5 Terminal2\terminal64.exe" \
     --ai-client none \
     > work/smc_bos_live.log 2>&1 &
   ```

   同様に `work/smc_bos_live.log` に warm-up 完了ログが出るまで待つ。

4. narrow_focus の起動完了を確認してから atr_trend_scalp を起動する(同時起動
   しない理由は手順2と同じ。smc_bos は凍結中のためこの間には挟まない):

   ```bash
   python -m infers.main --mode live --demo --strategy atr_trend_scalp --symbol XAUUSD \
     --tf M5 \
     --journal work/journal/atr_trend_scalp_xauusd.jsonl \
     --terminal-path "C:\Program Files\Vantage Trading MT5 Terminal3\terminal64.exe" \
     --ai-client none \
     --risk-pct 0.01 --min-volume-steps 1 \
     > work/atr_trend_scalp_live.log 2>&1 &
   ```

   同様に `work/atr_trend_scalp_live.log` に warm-up 完了ログが出るまで待つ。

5. 2ターミナル(narrow_focus・atr_trend_scalp)がそれぞれ別プロセス・別パスで
   起動していることを確認(これが一番重要な検証ポイント。smc_bos は凍結中
   のため起動していないことも確認する):

   ```bash
   powershell -Command "Get-CimInstance Win32_Process | Where-Object { \$_.Name -like '*terminal64*' } | Select-Object ProcessId,ExecutablePath"
   ```

   `Vantage Prime MT5 Terminal\terminal64.exe`・
   `Vantage Trading MT5 Terminal3\terminal64.exe` の2行が、別PIDで
   表示されればOK(`Vantage Trading MT5 Terminal2\terminal64.exe` = smc_bos用は
   凍結中のため表示されないのが正しい)。行数が想定と異なる・パスが重複している
   場合は相乗り事故が起きているので、手順1からやり直す。

## 補足

- `--ai-client none`(passthroughゲート)は smc_bos(凍結中)・atr_trend_scalp 用
  (いずれもルールベースゲートが narrow_focus 固有の特徴量を前提とするため
  GUARDRAIL NO_GO になる。spec.md 参照)。narrow_focus はルールベース
  ゲート前提のため付けない。
- atr_trend_scalp は `--tf M5` を明示する(既定値と同じだが、smc_bos で
  `--tf` 省略により M30 のはずが M5 で誤稼働した実例があるため、明示を
  習慣にする)。
- ジャーナルは `work/journal/narrow_focus_xauusd.jsonl` /
  `work/journal/smc_bos_xauusd.jsonl` /
  `work/journal/atr_trend_scalp_xauusd.jsonl` に追記される。ダッシュボード
  (`python -m infers.dashboard`)で読み取り専用に閲覧できる。
- プロセスが理由不明に落ちることがある(過去に発生)。定期的に
  手順1のプロセス確認コマンドで生存をチェックし、落ちていたら本手順で
  再起動する。

## 運用堅牢化(改善プラン T13、2026-07-04)

### OPS-1: ウォッチドッグ(PC再起動後の自動起動)

`scripts\start_live.ps1` は既に以下の安全弁を備えている:
- 冪等(既に起動中の手法は自動でスキップ)
- smc_bos は起動しない(ライブ凍結中)
- **narrow_focus・atr_trend_scalp は、ジャーナルに `KILL_SWITCH_ENGAGED` イベントが
  1件でも残っていれば自動起動しない**(`Test-KillSwitchEngaged` 関数)。キルスイッチは
  プロセス再起動でメモリ上はリセットされるが、それは「なぜ作動したか」を人間が確認した
  ことを意味しない。原因調査を経ずに自動再起動すると同じ事故を繰り返しかねないため、
  **原因調査後に該当ジャーナルファイルをアーカイブ/リネームしてから手動で再起動する**運用とする。

PC再起動後にこのスクリプトを自動実行させたい場合は `scripts\register_watchdog_task.ps1`
(Windows タスクスケジューラへ「ログオン時に start_live.ps1 を実行」するタスク
`INFERS-LiveWatchdog` を登録する)を**手動で実行**する(システム状態を変更するため
自動実行はしない設計。要管理者権限)。解除は
`Unregister-ScheduledTask -TaskName "INFERS-LiveWatchdog" -Confirm:$false`。

### OPS-2: ハートビート(無音検知)

各ライブプロセスは既定で5分ごとにジャーナルへ `HEARTBEAT` イベントを記録する
(`LiveRunner` の `heartbeat_interval_s` 引数。既定300秒、0で無効)。確定足が長時間
流れない(フィード無音)障害は、バー駆動の記録だけでは検知できない(バーが来ないこと
自体が障害のため)。ハートビートはバーの到着に依存しない独立スレッドで駆動するため、
この種の障害でも生存確認ができる。

ダッシュボード(`python -m infers.dashboard`)は各ジャーナルの最終イベントからの
経過時間(`silence_seconds`)を表示し、900秒(ハートビート既定間隔の3倍)を超えると
赤字で「⚠ 無音 N分」と警告する(表示のみ・自動アクションなし)。

**確定足の停止検知(2026-11 追加)**: ハートビートは生存確認に加えて
`last_bar_close`(最後に処理した確定足)・`lag_s`(その遅れ)・`feed_active_at`
(フィードが最後に価格変化を観測した時刻)を記録する。「価格は動いている(直近 3×TF 以内に
変化あり)のに確定足が 3×TF 以上来ない」状態になると `FEED_STALE` を1回記録し、確定足が
戻れば `FEED_RESUMED` を記録する(記録のみ・自動アクションなし)。週末は価格が動かないので
誤報しない。2026-07-05/12 の週末起動では、プロセスは生きていてハートビートも出ていたが、
確定足が21時間〜3日間止まっていた(下記「週末起動」の不具合)。

## 異常終了・発注失敗の記録(2026-11 追加)

2026-07 のデモ稼働では、GO 判断の直後にプロセスが終了しトレースバックも残らなかった
(指値の成功コード 10008 を失敗扱い・注文コメントが MT5 上限 31 文字超過・発注失敗の
未捕捉)。修正後の挙動:

- **ログは上書きしない**: `start_live.ps1` は起動ごとに `work\logs\<手法>_<日時>.log` /
  `.err.log` を新規作成する(`python -u` で即時書き出し)。旧 `work\*_live*.log` は使わない。
- **ジャーナルの新イベント**:
  - `ORDER_REJECT`: 新規建ての発注が拒否された。見送って稼働を続ける(キルスイッチなし)。
  - `ORDER_ERROR`: 決済・SL変更・取消・追加の失敗。キルスイッチ(新規停止)+リコンサイル。
    原因調査後、手順どおりジャーナルをアーカイブしてから再起動する。
  - `CRASH`: 未捕捉例外での終了直前に、型・メッセージ・トレースバックを記録する。
  - `SESSION` に `git_commit`/`git_branch`/`git_dirty` を記録する(稼働コードの特定用)。
    **稼働中に同じチェックアウトでブランチを切り替えない**(07-02 に稼働中の checkout で
    atr のコードがディスクから消え、GO 時の遅延 import が失敗した疑いがある)。
- **注文コメント**: `<position_id のハッシュ10桁>/<seq>/<tag>`(31文字以内)。MT5 の注文一覧で
  見えるコメントは client_order_id そのものではない。

## 週末起動(2026-11 修正)

サーバー時刻オフセットは「ティックが前進している(=市場が開いている)」ときだけ確定する。
週末・休場中に起動すると `waiting for market open to determine server UTC offset...` を
出して開場まで待機し(ジャーナルに `OFFSET_WAIT`)、確定後にウォームアップと稼働を始める
(確定値は `FEED_OFFSET` に記録)。`start_live.ps1` はこの待機を正常起動として扱う。
週末に `--mode export` する場合は `--server-utc-offset <時間>` の明示が必要
(Vantage は米国夏時間連動で UTC+3/冬時間 UTC+2)。

### OPS-3: 日次リコンサイル(ジャーナル vs MT5実約定の突合)

```
python tools/daily_reconciliation.py work/journal/atr_trend_scalp_xauusd.jsonl \
  --terminal-path "C:\Program Files\Vantage Trading MT5 Terminal3\terminal64.exe" \
  --symbol XAUUSD --day 2026-07-04
```

ジャーナルの退出系FSMイベント(SL_HIT/TP_CLOSE/CLOSE_ALL)の件数と、MT5の実約定履歴
(`history_deals_get`)の退出件数を突合し、乖離があれば警告する。**表示のみ・自動修復は
しない**(smc_bos のファントム建玉事故のような「内部状態と実口座の乖離が長期間気づかれ
なかった」問題の早期検知が目的)。日次でこのコマンドを実行する運用を推奨する
(タスクスケジューラでの自動化は任意。本ツール自体は診断のみで発注APIに触れない)。
