# ruff: noqa: E701, E702, I001, W191
# fmt: off
from __future__ import annotations;import math;from collections.abc import Mapping;from datetime import date,timedelta;from typing import Any;from moex_spread_lab.core._fastcore_loader import load_fastcore;from moex_spread_lab.core.contracts import EntryBandPoint;from..base import CALENDAR_EXIT_REASONS,calendar_row_buffers,make_calendar_trade;QUARTER_DAYS=91;SBER_EX_DATES:tuple[tuple[date,float],...]=((date(2023,5,9),25.),(date(2024,7,9),33.3),(date(2025,7,16),34.84),(date(2026,7,18),37.64));DIVIDEND_SHARES_PER_CONTRACT=100;_CONVERGENCE_EXTRA_OPTS=frozenset({'exit_days','close_at_end','z_cache','z_cache_key'})
def window_dividend_rub(asset:str,expiry_date:date)->float:
	if asset!='SR':return .0
	return sum(b for(a,b)in SBER_EX_DATES if expiry_date<a<=expiry_date+timedelta(days=QUARTER_DAYS))*DIVIDEND_SHARES_PER_CONTRACT
def implied_ruonia_deviation_pp(spread_value:float,near_value:float,dividend_rub:float,ruonia_rate:float|None,*,convergence_supported:bool=True)->float:
	if not convergence_supported or ruonia_rate is None or near_value<=0:return math.nan
	a=(spread_value+dividend_rub)/near_value*365/QUARTER_DAYS;return(a-ruonia_rate)*100
def _terminal_mark(rows:list[dict[str,Any]],entry:dict[str,Any],position:int,fill:str,close_at_end:bool,signal_row:dict[str,Any])->dict[str,Any]:
	a='dev_pp',signal_row,float(signal_row['dev_pp'])
	if close_at_end and entry is not rows[-1]:return make_calendar_trade(entry,rows[-1],position,fill,'end_of_window',a)
	return{'entry':entry,'exit':rows[-1],'direction':position,'signal':a}
def _convergence_trades_py(rows:list[dict[str,Any]],*,threshold:float,delay:int,fill:str,exit_days:int,close_at_end:bool)->list[dict[str,Any]]:
	position=0;entry:dict[str,Any]|None=None;signal_row:dict[str,Any]|None=None;trades:list[dict[str,Any]]=[]
	for(c,a)in enumerate(rows):
		if position and entry is not None and signal_row is not None and a.get('exit_eligible',True)and a['days_to_near_expiry']<=exit_days:trades.append(make_calendar_trade(entry,a,position,fill,'near_expiry_buffer',('dev_pp',signal_row,float(signal_row['dev_pp']))));position=0
		b=rows[c-delay]if c>=delay else None
		if not position and b is not None and b.get('signal_eligible',True)and a.get('entry_eligible',True)and a['days_to_near_expiry']>exit_days and not math.isnan(float(b['dev_pp']))and abs(float(b['dev_pp']))>=threshold:position=-1 if float(b['dev_pp'])>0 else 1;entry=a;signal_row=b
	if position and entry is not None and signal_row is not None:trades.append(_terminal_mark(rows,entry,position,fill,close_at_end,signal_row))
	return trades
def convergence_trades(rows:list[dict[str,Any]],*,threshold:float,delay:int,fill:str,**options:Any)->list[dict[str,Any]]:
	a=set(options)-_CONVERGENCE_EXTRA_OPTS
	if a:raise TypeError(f"unexpected keyword argument(s): {sorted(a)}")
	exit_days=int(options.get('exit_days',2));close_at_end=bool(options.get('close_at_end',False));z_cache:dict[Any,Any]|None=options.get('z_cache');z_cache_key:Any=options.get('z_cache_key')
	if not rows or rows[0]['dividend_in_window']:return[]
	b=load_fastcore()
	if b is not None and delay>=0:_,d,_,e,f,g,h,_=calendar_row_buffers(rows,z_cache=z_cache,z_cache_key=z_cache_key);specs:list[tuple[int,int,int,int,int,float]]=b.calendar_convergence_scan(d,e,f,g,h,float(threshold),int(delay),exit_days,close_at_end);return[make_calendar_trade(rows[a],rows[b],c,fill,CALENDAR_EXIT_REASONS[d],('dev_pp',rows[e],f))for(a,b,c,d,e,f)in specs]
	return _convergence_trades_py(rows,threshold=threshold,delay=delay,fill=fill,exit_days=exit_days,close_at_end=close_at_end)
def compute_ruonia_entry_bands(series:list[dict[str,Any]],params:Mapping[str,Any],fill:str,*,far_point_value_rub:float=1.)->list[EntryBandPoint]:
	del fill,far_point_value_rub;threshold=float(params['threshold_pp']);points:list[EntryBandPoint]=[]
	for a in series:
		g=str(a.get('date',''))
		if not a.get('volume_eligible',True):points.append(EntryBandPoint(date=g));continue
		b=a.get('dev_pp');c=a.get('rate_price_per_pp');d=e=h=i=None
		if b is not None and c is not None and math.isfinite(float(b)):
			j=float(b);k=float(a['spread']);d=k+(-threshold-j)*float(c);e=k+(threshold-j)*float(c);f=float(a['far'])
			if f:h=d/f*100;i=e/f*100
		points.append(EntryBandPoint(date=g,buy_level=d,sell_level=e,buy_level_rel=h,sell_level_rel=i))
	return points
__all__=['DIVIDEND_SHARES_PER_CONTRACT','QUARTER_DAYS','SBER_EX_DATES','compute_ruonia_entry_bands','convergence_trades','implied_ruonia_deviation_pp','window_dividend_rub']
# fmt: on
