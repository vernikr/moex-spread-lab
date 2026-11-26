# ruff: noqa: E701, E702, I001, W191
# fmt: off
from __future__ import annotations;import math;from collections.abc import Sequence;from dataclasses import dataclass,field;from typing import Any;from..base import clean_nonfinite_json,elapsed_calendar_days;from..calendar_ruonia.engine import QUARTER_DAYS;_COMPACT_KEYS='date session_date benchmark_mark_date near far near_open far_open spread volume_eligible entry_eligible exit_eligible point_value_rub';_EVENT_KEYS='date spread_fill spread_fill_rel near_price far_price spread_volume near_volume far_volume costs';_FULL_KEYS='date timestamp session_date benchmark_mark_date timeframe near far near_open far_open spread spread_open spread_rel spread_open_rel spread_last_trade_rel spread_last_trade spread_volume volume_eligible signal_eligible entry_eligible exit_eligible spread_trades near_volume far_volume days_to_near_expiry dev_pp ruonia_rate_pct ruonia_rate_date rate_price_per_pp margin_rub point_value_rub';_LEG_KEYS='date spread_fill near_price far_price';_TRADE_KEYS='number side signal entry exit reason'
def _row(keys:str,values:Sequence[Any])->dict[str,Any]:return dict(zip(keys.split(),values,strict=True))
def rel_pct(value:Any,base:Any)->float|None:
	try:b=float(value);a=float(base)
	except(TypeError,ValueError):return None
	if not math.isfinite(b)or not math.isfinite(a)or a==.0:return None
	return round(b/a*100,6)
clean_payload=clean_nonfinite_json
def elapsed_days(start:str,end:str)->float:return round(elapsed_calendar_days(start,end),4)
@dataclass
class _LedgerSim:quantity:int;by_entry:dict[str,dict[str,Any]];by_exit:dict[str,dict[str,Any]];marked:dict[int,float];position:dict[str,Any]|None=None;cumulative:float=.0;ledger:list[dict[str,Any]]=field(default_factory=list)
def _step_open_position(sim:_LedgerSim,row:dict[str,Any],day:str,point:float,vol_ok:bool,ext_ok:bool)->tuple[float,float]:
	if sim.position is None:return .0,.0
	a=sim.by_exit.get(day);d=a is not None and a['reason']=='end_of_window'
	if a is not None and(ext_ok or d):end=float(a['exit']['spread_fill']);b=sim.position['sign']*(end-sim.position['mark'])*point;sim.marked[sim.position['number']]+=b;e=sum(a['exit']['costs'].values());sim.position=None;return b,e
	if vol_ok:c=float(row['spread_price']);b=sim.position['sign']*(c-sim.position['mark'])*point;sim.marked[sim.position['number']]+=b;sim.position['mark']=c;return b,.0
	return .0,.0
def _step_entry_position(sim:_LedgerSim,row:dict[str,Any],day:str,point:float,vol_ok:bool,ent_ok:bool)->tuple[float,float]:
	a=sim.by_entry.get(day)if ent_ok else None
	if not a:return .0,.0
	c=1 if a['side']=='long_spread'else-1;g=sum(a['entry']['costs'].values());d=float(a['entry']['spread_fill'])
	if vol_ok:e=float(row['spread_price']);b=c*(e-d)*point;sim.marked[a['number']]+=b;f=e
	else:b,f=.0,d
	sim.position={'sign':c,'mark':f,'number':int(a['number'])};return b,g
def simulate_marked_ledger(rows:list[dict[str,Any]],trades:list[dict[str,Any]],quantity:int,*,compact:bool)->tuple[list[dict[str,Any]],dict[int,float],float]:
	sim=_LedgerSim(quantity=quantity,by_entry={a['entry']['date']:a for a in trades},by_exit={a['exit']['date']:a for a in trades},marked={int(a['number']):.0 for a in trades})
	for row in rows:day=row['date'];point=float(row['mult'])*quantity;vol_ok=bool(row.get('volume_eligible',row.get('min_volume')is not None and row['min_volume']>0));ent_ok=bool(row.get('entry_eligible',vol_ok));ext_ok=bool(row.get('exit_eligible',vol_ok));c,d=_step_open_position(sim,row,day,point,vol_ok,ext_ok);e,f=_step_entry_position(sim,row,day,point,vol_ok,ent_ok);a,b=c+e,d+f;sim.cumulative+=a-b;sim.ledger.append(_row('date cumulative_net_rub',(day,round(sim.cumulative,2)))if compact else _row('date position quantity mark_pnl_rub costs_rub cumulative_net_rub',(day,sim.position['sign']if sim.position else 0,sim.quantity if sim.position else 0,round(a,2),round(b,2),round(sim.cumulative,2))))
	return sim.ledger,sim.marked,sim.cumulative
def compact_series(rows:list[dict[str,Any]],series_cache:dict[Any,Any]|None,series_key:Any)->list[dict[str,Any]]:
	if series_cache is not None and series_key in series_cache:return series_cache[series_key]
	compact=[_row(_COMPACT_KEYS,(r['date'],r.get('session_date',r['date'][:10]),r.get('benchmark_mark_date'),r['near_price'],r['far_price'],r['near_open_price'],r['far_open_price'],r['spread_price'],r.get('volume_eligible',r['min_volume']>0),r.get('entry_eligible',r.get('volume_eligible',r['min_volume']>0)),r.get('exit_eligible',r.get('volume_eligible',r['min_volume']>0)),r['mult']))for r in rows]
	if series_cache is not None:series_cache[series_key]=compact
	return compact
def full_event_dict(row:dict[str,Any],fee:dict[str,float],is_open:bool)->dict[str,Any]:a=row['spread_open_price']if is_open else row['spread_price'];b=row['far_open_price']if is_open else row['far_price'];c=row['near_open_price']if is_open else row['near_price'];return _row(_EVENT_KEYS,(row['date'],a,rel_pct(a,b),c,b,row.get('spread_volume',row['min_volume']),row['near_volume'],row['far_volume'],fee))
def full_series_row(r:dict[str,Any],tf:str,quantity:int)->dict[str,Any]:vol_ok=r.get('volume_eligible',r['min_volume']>0);a=r.get('ruonia_rate');return _row(_FULL_KEYS,(r['date'],r.get('timestamp',r['date']),r.get('session_date',r['date'][:10]),r.get('benchmark_mark_date'),tf,r['near_price'],r['far_price'],r['near_open_price'],r['far_open_price'],r['spread_price'],r['spread_open_price'],rel_pct(r['spread_price'],r['far_price']),rel_pct(r['spread_open_price'],r['far_open_price']),rel_pct(r['spread_last_trade'],r['far_price']),r['spread_last_trade'],r.get('spread_volume',r['min_volume']),vol_ok,r.get('signal_eligible',vol_ok),r.get('entry_eligible',vol_ok),r.get('exit_eligible',vol_ok),r['spread_trades'],r['near_volume'],r['far_volume'],r['days_to_near_expiry'],r['dev_pp'],float(a)*100 if a is not None else None,r.get('ruonia_rate_date'),r['near_price']*QUARTER_DAYS/365/100,round(r['margin']*quantity,2),r['mult']))
def sizing_intents_trades(account:dict[str,Any],is_open:bool)->list[dict[str,Any]]:
	a='spread_open_price'if is_open else'spread_price';c='near_open_price'if is_open else'near_price';d='far_open_price'if is_open else'far_price'
	def b(row:dict[str,Any])->dict[str,Any]:return _row(_LEG_KEYS,(row['date'],row[a],row[c],row[d]))
	return[_row(_TRADE_KEYS,(c,'short_spread'if a['direction']<0 else'long_spread',a['signal'],b(a['entry_row']),b(a['exit_row']),a['reason']))for(c,a)in enumerate(account['trade_list'],start=1)]
# fmt: on
