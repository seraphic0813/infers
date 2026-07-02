# ライブ監視 起動手順(PC再起動後など)

narrow_focus・smc_bos・atr_trend_scalp の3手法を、別々のMT5デモ口座(別ターミナル
インストール)で同時にライブ監視するための手順。PCを再起動するとプロセスは
残らないため、毎回この手順で再起動する。

**`scripts\start_live.bat`(ダブルクリック)/ `scripts\start_live.ps1` が
この手順を自動化している。** 通常はそちらを使えばよく、以下は内部で
何が起きているかの説明と、手動操作が必要な場合の手順。
停止は `scripts\stop_live.bat`(ダブルクリック)/ `scripts\stop_live.ps1` —
3手法のPythonプロセスと対応するMT5ターミナル3つをまとめて停止する。

## 前提

- narrow_focus → `Vantage Prime MT5 Terminal`(既定口座)
- smc_bos → `Vantage Trading MT5 Terminal2`(2口座目)
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

3. narrow_focus の起動完了を確認してから smc_bos を起動する(同時起動すると
   どちらのターミナルが先に立つか競合し、相乗り事故の再発リスクがあるため
   **同時に投げない**):

   ```bash
   python -m infers.main --mode live --demo --strategy smc_bos --symbol XAUUSD \
     --journal work/journal/smc_bos_xauusd.jsonl \
     --terminal-path "C:\Program Files\Vantage Trading MT5 Terminal2\terminal64.exe" \
     --ai-client none \
     > work/smc_bos_live.log 2>&1 &
   ```

   同様に `work/smc_bos_live.log` に warm-up 完了ログが出るまで待つ。

4. smc_bos の起動完了を確認してから atr_trend_scalp を起動する(同時起動
   しない理由は手順3と同じ):

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

5. 3ターミナルすべてが別プロセス・別パスで起動していることを確認(これが
   一番重要な検証ポイント):

   ```bash
   powershell -Command "Get-CimInstance Win32_Process | Where-Object { \$_.Name -like '*terminal64*' } | Select-Object ProcessId,ExecutablePath"
   ```

   `Vantage Prime MT5 Terminal\terminal64.exe`・
   `Vantage Trading MT5 Terminal2\terminal64.exe`・
   `Vantage Trading MT5 Terminal3\terminal64.exe` の3行が、別PIDで
   表示されればOK。行数が足りない・パスが重複している場合は相乗り事故が
   起きているので、手順1からやり直す。

## 補足

- `--ai-client none`(passthroughゲート)は smc_bos・atr_trend_scalp 用
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
