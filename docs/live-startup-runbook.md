# ライブ監視 起動手順(PC再起動後など)

narrow_focus・smc_bos の2手法を、別々のMT5デモ口座(別ターミナルインストール)で
同時にライブ監視するための手順。PCを再起動するとプロセスは残らないため、
毎回この手順で再起動する。

## 前提

- narrow_focus → `Vantage Prime MT5 Terminal`(既定口座)
- smc_bos → `Vantage Trading MT5 Terminal2`(2口座目)
- 2つのMT5ターミナルは別フォルダにインストール済み。`--terminal-path` を
  **必ず両方に明示指定**すること。省略すると `mt5.initialize()` が
  「既に起動中の別ターミナル」に相乗りしてしまい、2手法が同じ口座に
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

4. 両ターミナルが別プロセス・別パスで起動していることを確認(これが
   一番重要な検証ポイント):

   ```bash
   powershell -Command "Get-CimInstance Win32_Process | Where-Object { \$_.Name -like '*terminal64*' } | Select-Object ProcessId,ExecutablePath"
   ```

   `Vantage Prime MT5 Terminal\terminal64.exe` と
   `Vantage Trading MT5 Terminal2\terminal64.exe` の2行が、別PIDで
   表示されればOK。1行しか出ない・パスが片方しかない場合は相乗り事故が
   起きているので、手順1からやり直す。

## 補足

- `--ai-client none`(passthroughゲート)は smc_bos 用。narrow_focus は
  ルールベースゲート前提のため付けない。
- ジャーナルは `work/journal/narrow_focus_xauusd.jsonl` /
  `work/journal/smc_bos_xauusd.jsonl` に追記される。ダッシュボード
  (`python -m infers.dashboard`)で読み取り専用に閲覧できる。
- プロセスが理由不明に落ちることがある(過去に発生)。定期的に
  手順1のプロセス確認コマンドで生存をチェックし、落ちていたら本手順で
  再起動する。
