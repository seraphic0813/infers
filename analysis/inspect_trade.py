import json
import sys

raw = open('reports/rule_baseline/report_data.js', encoding='utf-8').read()
d = json.loads(raw.removeprefix('window.BT = ').rstrip(';\n'))
TICK = float(d['summary']['tick_size'])

idx = int(sys.argv[1]) - 1 if len(sys.argv) > 1 else 286
t = d['trades'][idx]
print(f"=== トレード #{idx+1}  id={t['id']} ===")
print(f"方向: {'買(long)' if t['dir']>0 else '売(short)'}")
print(f"exit_kind (台帳の最終退出種別): {t['exit_kind']}")
print(f"pnl_ts: {t['pnl_ts']}  (符号 {'+' if t['pnl_ts']>=0 else '-'})")
print()
print("entries (約定価格×数量):")
for tt, p, v in t['entries']:
    print(f"  price={p*TICK:.2f}  vol={v}")
print("exits (決済価格×数量):")
for tt, p, v in t['exits']:
    print(f"  price={p*TICK:.2f}  vol={v}")
print()
print("FSMジャーナル (実際に何が起きたか):")
for name, payload in t['journal']:
    keys = {k: v for k, v in payload.items() if k != 'state'}
    print(f"  {name}: {keys}")
