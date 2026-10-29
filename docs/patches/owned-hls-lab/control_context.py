"""Add native-action/callback context without altering audio/clock qualification."""
import json
from pathlib import Path

def add_control_context(report,capture,callbacks_path=None):
 capture=Path(capture);observation=json.load(open(capture/'kodi-clock-observation.json'));m=json.load(open(capture/'capture.json'));first=m['first_host_pts']['value'];actions=observation.get('actions',[]);events=[json.loads(x) for x in open(callbacks_path)] if callbacks_path and Path(callbacks_path).exists() else[];pauses=[x for x in actions if x['action']=='pause'];pairs=[]
 for pause in pauses:
  resume=next((x for x in actions if x.get('cycle')==pause.get('cycle') and x['action']=='resume'),None)
  if resume:pairs.append((pause,resume))
 for gap in report['internal_quiet_gaps']:
  start,end=gap['capture_seconds'];gap['intersects_requested_pause_cycles']=[pa['cycle'] for pa,re in pairs if max(start,(pa['request_host_ns']-first)/1e9)<min(end,(re['response_host_ns']-first)/1e9)]
 matched=[]
 mismatched={}
 for kind in ['pause','resume']:
  aa=[x for x in actions if x['action']==kind];ee=[x for x in events if x['event']==kind]
  if len(aa)!=len(ee):mismatched[kind]={'actions':len(aa),'callbacks':len(ee)}
  for action,event in zip(aa,ee):
   ns=event['monotonic_ns'];matched.append({'action':kind,'cycle':action['cycle'],'rpc_result':action.get('result'),'callback_host_ns':ns,'callback_delay_after_rpc_ms':[(ns-action['response_host_ns'])/1e6,(ns-action['request_host_ns'])/1e6]})
 report['control_context']={'actions':actions,'callbacks':events,'callback_latency':matched,'callback_count_mismatch':mismatched,'uncommanded_quiet_gap_count':sum(not x['intersects_requested_pause_cycles'] for x in report['internal_quiet_gaps']),'association_rule':'Gap intersects a requested Pause-to-Resume RPC interval; intersection alone does not prove correct native Pause behavior.','callback_clock_note':'Recorded values share this run Mach-time numeric range; use separately from core renderer-clock gate.'}
