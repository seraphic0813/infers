# INFERS

**I**ntelligent **N**arrow **F**ocus **E**lliot **R**ealtime **S**ystem — 「Narrow Focus トレード手法」をPython自動取引Bot+長期バックテストとしてシステム実装するプロジェクト。

## ドキュメント

- [フェーズ1: システム基本設計図(アーキテクチャ)](docs/phase1-architecture.md)
  - 2層分析(マクロ/ミクロ)のデータ構造定義
  - 「未来裁量」の数理アルゴリズム(SMA前方投影の閉形式解・RSI逆算)
  - 資金管理・防御策の状態機械設計
  - ハイブリッドAI判断層(Python L0 / Haiku 4.5 L1 / Fable 5 L2)とコスト試算

## ステータス

フェーズ1(基本設計)完了。フェーズ2以降の実装ロードマップは設計書 §10 を参照。
