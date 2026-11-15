//+------------------------------------------------------------------+
//| AtrTrendScalp.mq5                                                |
//| atr_trend_scalp v1.3 の MQL5 独立実装(検証用 EA)                 |
//|                                                                  |
//| 目的: Python 実装(src/infers/strategies/atr_trend_scalp/)の       |
//| バックテスト成績を、MT5 ストラテジーテスター(ブローカーと同じ    |
//| 約定ルール・実ティック)で独立に再現・突き合わせる。               |
//| ロジックの正は spec.md §2・§3・§19。Python と同じ規則を、         |
//| Python のコードを見ずに書ける粒度で移植している。                 |
//|                                                                  |
//| Python との意図的な違い(現実の約定に寄せる部分):                 |
//|  - 約定は実際の Bid/Ask(固定スプレッド2ティックではない)         |
//|  - 売り建玉の SL/TP 判定はブローカー側(Ask 基準)                 |
//|  - 「置けない SL」の判定は、確定足終値ではなく現在の Bid/Ask と   |
//|    銘柄のストップレベルで行う                                     |
//+------------------------------------------------------------------+
#property copyright "INFERS"
#property version   "1.30"
#property strict

#include <Trade/Trade.mqh>

//--- 入力(既定値 = v1.3 プリセット atr_trend_scalp_v13)
input int    InpEmaFast        = 9;
input int    InpEmaMedium      = 21;
input int    InpEmaTrend       = 50;     // 上位足(M15)EMA
input int    InpHtfMinutes     = 15;     // 上位足の分数(M5 から自前で集約)
input int    InpAtrPeriod      = 14;
input int    InpVolSmaPeriod   = 20;
input int    InpAtrAvgPeriod   = 20;
input int    InpRetraceLookback= 3;
input double InpAtrVolMult     = 1.1;
input double InpVolMult        = 1.6;
input double InpSlAtrMult      = 1.0;
input double InpTp1AtrMult     = 0.4;    // v1.3
input double InpTp2AtrMult     = 3.0;
input double InpTrailAtrMult   = 0.15;
input int    InpMinStopTicks   = 5;
input double InpAtrFloorBps    = 5.0;
input double InpLots           = 0.02;   // バックテスト(最低2ステップ)と同じ
input int    InpMaxSpreadTicks = 50;     // これを超えるスプレッドでは新規見送り
input int    InpWarmupBars     = 3000;   // 開始前の履歴で指標を温める本数
input long   InpMagic          = 20261115;
input string InpTradeLog       = "atr_trend_scalp_ea_trades.csv";

//--- 指標状態(Python の infers.indicators と同じ定義: SMA で種まき→指数/ワイルダー平滑)
struct Ema {
   int period; double k; double value; bool ready; double seedSum; int seedCount;
};
struct Atr {
   int period; double value; bool ready; bool hasPrev; double prevClose; double seed[]; int seedCount;
};

Ema    g_emaFast, g_emaMed, g_emaTrend;
Atr    g_atr;
double g_volBuf[];  int g_volCount = 0;      // 出来高 SMA20 用リング
double g_atrWin[];  int g_atrWinCount = 0;   // 直近20本の ATR
bool   g_retBelow[], g_retAbove[]; int g_retCount = 0;

//--- 上位足の自前集約(M5 → M15。Python TfResampler と同じ: 次バケットの最初の M5 で確定)
datetime g_htfBucket = 0; double g_htfClose = 0; bool g_htfHasOpen = false;
double   g_htfLastClosed = 0; bool g_htfClosedReady = false;

//--- 建玉状態(Enum 一本: IDLE/OPEN/RUNNER)
enum PosState { ST_IDLE = 0, ST_OPEN = 1, ST_RUNNER = 2 };
PosState g_state = ST_IDLE;
int      g_dir = 0;
double   g_entry = 0, g_sl = 0, g_tp1 = 0, g_tp2 = 0, g_trail = 0;
datetime g_entryTime = 0;
ulong    g_ticket = 0;

datetime g_lastBar = 0;
CTrade   g_trade;
int      g_log = INVALID_HANDLE;
double   g_tick = 0;

//+------------------------------------------------------------------+
void EmaInit(Ema &e, int p) { e.period = p; e.k = 2.0 / (p + 1); e.value = 0; e.ready = false; e.seedSum = 0; e.seedCount = 0; }
void EmaUpdate(Ema &e, double x)
{
   if(e.ready) { e.value = e.value + e.k * (x - e.value); return; }
   e.seedSum += x; e.seedCount++;
   if(e.seedCount >= e.period) { e.value = e.seedSum / e.period; e.ready = true; }
}
void AtrInit(Atr &a, int p) { a.period = p; a.value = 0; a.ready = false; a.hasPrev = false; a.seedCount = 0; ArrayResize(a.seed, p); }
void AtrUpdate(Atr &a, double h, double l, double c)
{
   double tr = h - l;
   if(a.hasPrev) tr = MathMax(h - l, MathMax(MathAbs(h - a.prevClose), MathAbs(l - a.prevClose)));
   a.prevClose = c; a.hasPrev = true;
   if(a.ready) { a.value = (a.value * (a.period - 1) + tr) / a.period; return; }
   a.seed[a.seedCount++] = tr;
   if(a.seedCount >= a.period) { double s = 0; for(int i = 0; i < a.period; i++) s += a.seed[i]; a.value = s / a.period; a.ready = true; }
}
void PushRing(double &buf[], int &count, int cap, double x)
{
   if(count < cap) { buf[count++] = x; return; }
   for(int i = 1; i < cap; i++) buf[i - 1] = buf[i];
   buf[cap - 1] = x;
}
void PushRetrace(bool below, bool above)
{
   int cap = InpRetraceLookback;
   if(g_retCount < cap) { g_retBelow[g_retCount] = below; g_retAbove[g_retCount] = above; g_retCount++; return; }
   for(int i = 1; i < cap; i++) { g_retBelow[i - 1] = g_retBelow[i]; g_retAbove[i - 1] = g_retAbove[i]; }
   g_retBelow[cap - 1] = below; g_retAbove[cap - 1] = above;
}
void HtfPush(datetime t, double c)
{
   datetime bucket = (datetime)((long)t / (InpHtfMinutes * 60) * (InpHtfMinutes * 60));
   if(g_htfHasOpen && bucket != g_htfBucket)
   {  // 直前バケットが確定
      EmaUpdate(g_emaTrend, g_htfClose);
      g_htfLastClosed = g_htfClose; g_htfClosedReady = true;
   }
   if(!g_htfHasOpen || bucket != g_htfBucket) { g_htfBucket = bucket; g_htfHasOpen = true; }
   g_htfClose = c;
}

//--- 確定足1本ぶん指標を進める(Python provider.on_candle の前半と同じ順序)
void FeedBar(const MqlRates &r)
{
   EmaUpdate(g_emaFast, r.close);
   EmaUpdate(g_emaMed, r.close);
   AtrUpdate(g_atr, r.high, r.low, r.close);
   PushRing(g_volBuf, g_volCount, InpVolSmaPeriod, (double)r.tick_volume);
   HtfPush(r.time, r.close);
   if(g_atr.ready) PushRing(g_atrWin, g_atrWinCount, InpAtrAvgPeriod, g_atr.value);
   bool below = false, above = false;
   if(g_emaMed.ready) { below = r.low <= g_emaMed.value; above = r.high >= g_emaMed.value; }
   PushRetrace(below, above);
}

//+------------------------------------------------------------------+
int OnInit()
{
   g_tick = SymbolInfoDouble(_Symbol, SYMBOL_TRADE_TICK_SIZE);
   EmaInit(g_emaFast, InpEmaFast); EmaInit(g_emaMed, InpEmaMedium); EmaInit(g_emaTrend, InpEmaTrend);
   AtrInit(g_atr, InpAtrPeriod);
   ArrayResize(g_volBuf, InpVolSmaPeriod); ArrayResize(g_atrWin, InpAtrAvgPeriod);
   ArrayResize(g_retBelow, InpRetraceLookback); ArrayResize(g_retAbove, InpRetraceLookback);
   g_trade.SetExpertMagicNumber(InpMagic);
   g_trade.SetDeviationInPoints(50);

   // 開始前の履歴で指標を温める(発注しない。Python ライブのウォームアップと同じ考え方)
   MqlRates rates[];
   ArraySetAsSeries(rates, false);
   int n = CopyRates(_Symbol, PERIOD_M5, 1, InpWarmupBars, rates);
   if(n <= 0) { Print("warm-up: CopyRates failed ", GetLastError()); return INIT_FAILED; }
   for(int i = 0; i < n; i++) FeedBar(rates[i]);
   g_lastBar = iTime(_Symbol, PERIOD_M5, 0);
   PrintFormat("warm-up: fed %d M5 bars (last %s)", n, TimeToString(rates[n - 1].time));

   g_log = FileOpen(InpTradeLog, FILE_WRITE | FILE_CSV | FILE_ANSI | FILE_COMMON, ',');
   if(g_log != INVALID_HANDLE)
      FileWrite(g_log, "entry_time", "dir", "entry", "sl0", "tp1", "tp2", "trail",
                "event_time", "event", "price", "volume");
   return INIT_SUCCEEDED;
}
void OnDeinit(const int reason)
{
   if(g_log != INVALID_HANDLE) FileClose(g_log);
   // 実約定(ブローカー/テスターの約定履歴)を CSV へ。Python との突き合わせはこちらを正とする。
   if(!HistorySelect(0, TimeCurrent() + 86400)) return;
   int h = FileOpen("atr_trend_scalp_ea_deals.csv", FILE_WRITE | FILE_CSV | FILE_ANSI | FILE_COMMON, ',');
   if(h == INVALID_HANDLE) return;
   FileWrite(h, "time", "position_id", "type", "entry", "price", "volume", "profit", "swap", "commission", "comment");
   int total = HistoryDealsTotal();
   for(int i = 0; i < total; i++)
   {
      ulong d = HistoryDealGetTicket(i);
      if(HistoryDealGetInteger(d, DEAL_MAGIC) != InpMagic) continue;
      FileWrite(h, TimeToString((datetime)HistoryDealGetInteger(d, DEAL_TIME), TIME_DATE | TIME_SECONDS),
                HistoryDealGetInteger(d, DEAL_POSITION_ID), HistoryDealGetInteger(d, DEAL_TYPE),
                HistoryDealGetInteger(d, DEAL_ENTRY), DoubleToString(HistoryDealGetDouble(d, DEAL_PRICE), 2),
                DoubleToString(HistoryDealGetDouble(d, DEAL_VOLUME), 2),
                DoubleToString(HistoryDealGetDouble(d, DEAL_PROFIT), 2),
                DoubleToString(HistoryDealGetDouble(d, DEAL_SWAP), 2),
                DoubleToString(HistoryDealGetDouble(d, DEAL_COMMISSION), 2),
                HistoryDealGetString(d, DEAL_COMMENT));
   }
   FileClose(h);
}

void Log(string ev, double price, double vol)
{
   if(g_log == INVALID_HANDLE) return;
   FileWrite(g_log, TimeToString(g_entryTime, TIME_DATE | TIME_MINUTES), g_dir,
             DoubleToString(g_entry, 2), DoubleToString(g_sl, 2), DoubleToString(g_tp1, 2),
             DoubleToString(g_tp2, 2), DoubleToString(g_trail, 2),
             TimeToString(TimeCurrent(), TIME_DATE | TIME_SECONDS), ev, DoubleToString(price, 2),
             DoubleToString(vol, 2));
}

//--- 自分の建玉を探す(無ければ IDLE に戻す = SL がブローカー側で約定した)
bool SelectMyPosition()
{
   for(int i = PositionsTotal() - 1; i >= 0; i--)
   {
      ulong t = PositionGetTicket(i);
      if(PositionSelectByTicket(t) && PositionGetString(POSITION_SYMBOL) == _Symbol
         && PositionGetInteger(POSITION_MAGIC) == InpMagic) { g_ticket = t; return true; }
   }
   return false;
}

double Round(double x) { return MathRound(x / g_tick) * g_tick; }
double ProfitSide(double a, double b) { return g_dir * (a - b); }

void CloseVolume(double vol, string ev)
{
   double px = (g_dir > 0) ? SymbolInfoDouble(_Symbol, SYMBOL_BID) : SymbolInfoDouble(_Symbol, SYMBOL_ASK);
   if(g_trade.PositionClosePartial(g_ticket, vol)) Log(ev, g_trade.ResultPrice() > 0 ? g_trade.ResultPrice() : px, vol);
   else PrintFormat("close failed (%s): retcode=%d", ev, g_trade.ResultRetcode());
}

//--- SL を前進。置けない位置(現在値・ストップレベルを越える)なら成行決済(spec.md §19)
bool ProtectOrExit(double target)
{
   if(ProfitSide(target, g_sl) <= 0) return false;          // 改善でなければ何もしない(SL単調性)
   double mkt = (g_dir > 0) ? SymbolInfoDouble(_Symbol, SYMBOL_BID) : SymbolInfoDouble(_Symbol, SYMBOL_ASK);
   double stopsLevel = SymbolInfoInteger(_Symbol, SYMBOL_TRADE_STOPS_LEVEL) * _Point;
   if(ProfitSide(mkt, target) <= stopsLevel)
   {
      CloseVolume(PositionGetDouble(POSITION_VOLUME), "STOP_AT_MARKET");
      g_state = ST_IDLE;
      return true;
   }
   if(g_trade.PositionModify(g_ticket, target, 0.0)) { g_sl = target; Log("MOVE_SL", target, 0); }
   else PrintFormat("modify failed: retcode=%d target=%.2f mkt=%.2f", g_trade.ResultRetcode(), target, mkt);
   return false;
}

//--- 建玉管理(Python AtrTrendExecution.on_bar と同じ規則)
void ManagePosition(const MqlRates &bar)
{
   if(g_state == ST_IDLE) return;
   if(!SelectMyPosition())
   {  // ブローカー側で SL 約定済み
      Log("SL_HIT", g_sl, 0);
      g_state = ST_IDLE; return;
   }
   double touch = (g_dir > 0) ? bar.high : bar.low;
   if(g_state == ST_OPEN && ProfitSide(touch, g_tp1) >= 0)
   {
      double vol = PositionGetDouble(POSITION_VOLUME);
      double step = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_STEP);
      double half = MathFloor(vol / 2.0 / step) * step;
      if(half >= step) { CloseVolume(half, "TP1"); SelectMyPosition(); }
      g_state = ST_RUNNER;
      if(ProtectOrExit(g_entry)) return;                      // 建値化
   }
   if(g_state == ST_RUNNER)
   {
      if(ProfitSide(touch, g_tp2) >= 0)
      {
         CloseVolume(PositionGetDouble(POSITION_VOLUME), "TP2");
         g_state = ST_IDLE; return;
      }
      ProtectOrExit(touch - g_dir * g_trail);
   }
}

//--- エントリー判定(Python provider.on_candle の後半と同じ規則)
void TryEntry(const MqlRates &bar)
{
   if(g_state != ST_IDLE) return;
   if(!(g_emaFast.ready && g_emaMed.ready && g_atr.ready && g_volCount >= InpVolSmaPeriod
        && g_emaTrend.ready && g_htfClosedReady && g_atrWinCount >= InpAtrAvgPeriod)) return;
   int dir = 0;
   if(g_emaFast.value > g_emaMed.value && g_htfLastClosed > g_emaTrend.value) dir = +1;
   else if(g_emaFast.value < g_emaMed.value && g_htfLastClosed < g_emaTrend.value) dir = -1;
   else return;
   bool touched = false;
   for(int i = 0; i < g_retCount; i++) touched = touched || (dir > 0 ? g_retBelow[i] : g_retAbove[i]);
   bool rebounded = (dir > 0) ? bar.close > g_emaMed.value : bar.close < g_emaMed.value;
   if(!(touched && rebounded)) return;
   double atr = g_atr.value;
   double avgAtr = 0; for(int i = 0; i < InpAtrAvgPeriod; i++) avgAtr += g_atrWin[i]; avgAtr /= InpAtrAvgPeriod;
   if(atr < InpAtrVolMult * avgAtr) return;
   if(InpAtrFloorBps != 0 && atr * 10000.0 < InpAtrFloorBps * bar.close) return;
   double volSma = 0; for(int i = 0; i < InpVolSmaPeriod; i++) volSma += g_volBuf[i]; volSma /= InpVolSmaPeriod;
   if((double)bar.tick_volume < InpVolMult * volSma) return;

   double tp1d = Round(InpTp1AtrMult * atr), tp2d = Round(InpTp2AtrMult * atr);
   double trail = MathMax(g_tick, Round(InpTrailAtrMult * atr));
   double sld = MathMax(Round(InpSlAtrMult * atr), InpMinStopTicks * g_tick);
   if(!(tp2d > tp1d && tp1d >= g_tick)) return;

   double spreadTicks = (SymbolInfoDouble(_Symbol, SYMBOL_ASK) - SymbolInfoDouble(_Symbol, SYMBOL_BID)) / g_tick;
   if(spreadTicks > InpMaxSpreadTicks) return;

   double ref = bar.close;
   double sl = ref - dir * sld;
   bool ok = (dir > 0) ? g_trade.Buy(InpLots, _Symbol, 0.0, sl, 0.0, "atr_v13")
                       : g_trade.Sell(InpLots, _Symbol, 0.0, sl, 0.0, "atr_v13");
   if(!ok) { PrintFormat("entry failed: retcode=%d", g_trade.ResultRetcode()); return; }
   g_dir = dir; g_entry = g_trade.ResultPrice(); g_sl = sl;
   g_tp1 = ref + dir * tp1d; g_tp2 = ref + dir * tp2d; g_trail = trail;
   g_entryTime = bar.time; g_state = ST_OPEN;
   SelectMyPosition();
   Log("ENTRY", g_entry, InpLots);
}

//+------------------------------------------------------------------+
void OnTick()
{
   datetime t0 = iTime(_Symbol, PERIOD_M5, 0);
   if(t0 == g_lastBar) return;                 // 確定足が出た瞬間(新しい足の最初のティック)だけ処理
   // 前回処理以降に確定した足をすべて順に処理する(取りこぼし防止)
   MqlRates r[];
   ArraySetAsSeries(r, false);
   int n = CopyRates(_Symbol, PERIOD_M5, g_lastBar, t0 - 1, r);
   g_lastBar = t0;
   for(int i = 0; i < n; i++)
   {
      if(r[i].time >= t0) break;
      FeedBar(r[i]);
      ManagePosition(r[i]);
      TryEntry(r[i]);
   }
}
//+------------------------------------------------------------------+
