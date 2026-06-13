import json

raw = open('reports/rule_baseline/report_data.js', encoding='utf-8').read()
d = json.loads(raw.removeprefix('window.BT = ').rstrip(';\n'))
trades = d['trades']
total = sum(t['pnl_ts'] for t in trades)
closes = [t for t in trades if t['exit_kind'] == 'CLOSE']
close_sum = sum(t['pnl_ts'] for t in closes)
rest = total - close_sum
usd = 0.0001  # contract 100 のときの 1 tick*step

print('全トレード合計 pnl:', total, 'tick*steps  = ${:,.2f}'.format(total * usd))
print('END_OF_DATA強制決済(5件)合計:', close_sum, ' = ${:,.2f}'.format(close_sum * usd))
print('それ以外(1539件・全SL)合計:', rest, ' = ${:,.2f}'.format(rest * usd))
print()
rest_trades = [t for t in trades if t['exit_kind'] != 'CLOSE']
wins = sum(1 for t in rest_trades if t['pnl_ts'] > 0)
gw = sum(t['pnl_ts'] for t in rest_trades if t['pnl_ts'] > 0)
gl = -sum(t['pnl_ts'] for t in rest_trades if t['pnl_ts'] < 0)
print('残り1539件(=実際に手仕舞えたトレードのみ):')
print('  勝率={:.1f}%  PF={:.2f}'.format(wins / len(rest_trades) * 100, gw / gl))
print('  純損益=${:,.2f}'.format(rest * usd))
