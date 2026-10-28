#!/usr/bin/env python3
"""Independent captured-source matching and unfitted renderer-clock audit.

Requires NumPy and FFmpeg for analysis only. Full-reference waveform anchors
are established without consulting the clock under test. Held-out clock times
use a fixed .73+n second grid and each buffer's actual Mach timestamp.
"""
from pathlib import Path
from control_context import add_control_context
import argparse,bisect,collections,json,subprocess
import numpy as np
RATE=48000;WIDTH=1920

def direct(x,y):
 xx=float(x@x);yy=float(y@y)
 if xx<1e-15:return None
 return {'correlation':float(x@y/np.sqrt(max(xx*yy,1e-30))),'gain':float(x@y/xx),'relative_rms_error':float(np.sqrt(((x-y)@(x-y))/xx))}

def local_match(reference,template,guess,radius):
 lo=max(0,int(round(guess))-radius);hi=min(len(reference),int(round(guess))+radius+len(template));x=reference[lo:hi].astype(float);t=template.astype(float)
 dot=np.correlate(x,t,'valid');cs=np.r_[0.,np.cumsum(x*x)];energy=np.maximum(cs[len(t):]-cs[:-len(t)],1e-30);score=dot/np.sqrt(energy*max(float(t@t),1e-30));i=int(np.argmax(score))
 return {'source_start_sample':lo+i,'correlation':float(score[i]),'gain':float(dot[i]/energy[i])}

class FullReferenceMatcher:
 def __init__(self,reference):self.reference=reference;self.cache={}
 def global_match(self,template,ch):
  stride=4;t=template[::stride].astype(float);k=len(t)
  if ch not in self.cache:
   x=self.reference[::stride,ch].astype(float);size=1<<(len(x)+k-2).bit_length();cs=np.r_[0.,np.cumsum(x*x)];self.cache[ch]=(len(x),size,np.fft.rfft(x,size),np.maximum(cs[k:]-cs[:-k],1e-30))
  n,size,xf,e=self.cache[ch];dot=np.fft.irfft(xf*np.fft.rfft(t[::-1],size),size)[k-1:n];score=dot/np.sqrt(e*max(float(t@t),1e-30));i=int(np.argmax(score));refined=local_match(self.reference[:,ch],template,i*stride,24);other=score.copy();other[max(0,i-600):min(len(other),i+601)]=-np.inf;refined['coarse_nonlocal_correlation']=float(np.max(other));return refined

def main():
 ap=argparse.ArgumentParser(description=__doc__);ap.add_argument('--capture',required=True,type=Path);ap.add_argument('--reference-pcm',required=True,type=Path);ap.add_argument('--ledger',required=True,type=Path);ap.add_argument('--callbacks',type=Path);ap.add_argument('--threshold-ms',type=float,default=1024/48);args=ap.parse_args();p=args.capture.resolve();m=json.load(open(p/'capture.json'));assert m['status']=='completed';pcm=p/'capture-analysis.f32'
 if not pcm.exists():subprocess.run(['ffmpeg','-v','error','-y','-i',str(p/'audio.caf'),'-ac','2','-ar','48000','-f','f32le',str(pcm)],check=True)
 audio=np.memmap(pcm,dtype='<f4',mode='r').reshape(-1,2);ref=np.memmap(args.reference_pcm,dtype='<f4',mode='r').reshape(-1,2);assert len(audio)==m['total_frames'];matcher=FullReferenceMatcher(ref)
 timing=[json.loads(x) for x in open(p/'timing.jsonl')];ht=np.array([x['host_pts_ns'] for x in timing],dtype=np.int64);fo=np.array([x['frame_offset'] for x in timing],dtype=np.int64);first=int(ht[0]);errors=[];total=0
 for i,x in enumerate(timing):
  if x['frame_offset']!=total:errors.append('frame_offset_gap')
  total+=x['frame_count']
  if x['input_timestamp']['flags']&3!=3:errors.append('invalid_timestamp_flags')
  if x['discontinuity']:errors.append('reported_discontinuity')
  if i and int(ht[i])<=int(ht[i-1]):errors.append('nonmonotonic_host_time')
 if total!=len(audio):errors.append('frame_total_mismatch')
 def host_frame(ns):
  i=max(0,min(len(ht)-1,int(np.searchsorted(ht,ns,side='right'))-1));return int(fo[i])+(ns-int(ht[i]))*RATE/1e9
 def frame_host(frame):
  i=max(0,min(len(fo)-1,int(np.searchsorted(fo,frame,side='right'))-1));return int(ht[i])+round((frame-int(fo[i]))*1e9/RATE)
 rms=np.sqrt(np.mean(audio[:len(audio)//240*240].reshape(-1,240,2).astype(float)**2,axis=(1,2)));spans=[]
 for i,value in enumerate(rms):
  kind='quiet' if value<1e-4 else'audio'
  if spans and spans[-1]['kind']==kind:spans[-1]['end']=(i+1)*.005
  else:spans.append({'kind':kind,'start':i*.005,'end':(i+1)*.005})
 audible=[x for x in spans if x['kind']=='audio'];segments=[]
 for number,span in enumerate(audible):
  item={'index':number,'span':span,'channels':[]}
  for ch in range(2):
   anchors=[];duration=span['end']-span['start']
   if duration>=.04:
    fractions=[.2,.5,.8] if duration>=.25 else[.5]
    for fraction in fractions:
     center=(span['start']+fraction*duration)*RATE;ci=max(round(span['start']*RATE),min(round(center)-WIDTH//2,round(span['end']*RATE)-WIDTH));result=matcher.global_match(audio[ci:ci+WIDTH,ch],ch);result.update(capture_start_sample=ci,offset_samples=ci-result['source_start_sample']);anchors.append(result)
   item['channels'].append({'channel':ch,'anchors':anchors})
  segments.append(item);print('ANCHOR',number,span,[[ (a['offset_samples'],round(a['correlation'],5)) for a in c['anchors']] for c in item['channels']],flush=True)
 def find_segment(frame):
  sec=frame/RATE
  return next((s for s in segments if s['span']['start']<=sec<s['span']['end']),None)
 def capture_match(frame,ch,segment=None,radius=64):
  seg=segment or find_segment(frame);anchors=seg['channels'][ch]['anchors'] if seg else[]
  if not anchors:return None
  ci=round(frame)-WIDTH//2;xs=[a['capture_start_sample'] for a in anchors];ys=[a['offset_samples'] for a in anchors];offset=float(np.interp(ci,xs,ys));result=local_match(ref[:,ch],audio[ci:ci+WIDTH,ch],ci-offset,radius);result.update(capture_start_sample=ci,source_center_sample=result['source_start_sample']+WIDTH//2,offset_samples=ci-result['source_start_sample']);return result
 # Dense waveform identity checks use capture-only anchors, never renderer time.
 for seg in segments:
  for ch in range(2):
   values=[]
   for ci in range(round((seg['span']['start']+.04)*RATE),round((seg['span']['end']-.06)*RATE),960):
    result=capture_match(ci+WIDTH//2,ch,seg,16)
    if result:values.append(result)
   if values:seg['channels'][ch]['dense']={'windows':len(values),'step_samples':960,'window_samples':WIDTH,'minimum_correlation':min(x['correlation'] for x in values),'below_0975':sum(x['correlation']<.975 for x in values),'gain_range':[min(x['gain'] for x in values),max(x['gain'] for x in values)],'offset_range':[min(x['offset_samples'] for x in values),max(x['offset_samples'] for x in values)]}
 ends=[]
 for ch in range(2):
  firsta=segments[0]['channels'][ch]['anchors'][0];lasta=segments[-1]['channels'][ch]['anchors'][-1];out={'channel':ch}
  for label,start,offset in [('prefix',0,firsta['offset_samples']),('tail',len(ref)-960,lasta['offset_samples'])]:
   options=[]
   for delta in range(-16,17):
    ci=start+offset+delta
    if 0<=ci and ci+960<=len(audio):
     value=direct(ref[start:start+960,ch].astype(float),audio[ci:ci+960,ch].astype(float))
     if value:options.append(dict(value,capture_start_sample=ci,reference_start_sample=start,offset_samples=ci-start))
   out[label]=max(options,key=lambda v:v['correlation']) if options else None
  ends.append(out)
 gaps=[]
 for i,span in enumerate(spans):
  if span['kind']!='quiet' or i==0 or i==len(spans)-1:continue
  a,b=span['start'],span['end'];gap={'capture_seconds':[a,b],'duration_ms':(b-a)*1000,'channels':[]};pre=spans[i-1];post=spans[i+1]
  for ch in range(2):
   leftcenter=(max(pre['start']+.02,a-.1))*RATE;rightcenter=(min(post['end']-.02,b+.1))*RATE;before=capture_match(leftcenter,ch);after=capture_match(rightcenter,ch);interior=audio[round((a+.005)*RATE):round((b-.005)*RATE),ch].astype(float);entry={'channel':ch,'interior_rms':float(np.sqrt(np.mean(interior**2))) if len(interior) else None,'interior_peak':float(abs(interior).max()) if len(interior) else None,'before':before,'after':after}
   if before and after:entry['rms_source_edge_advance_ms']=(b-a-(after['offset_samples']-before['offset_samples'])/RATE)*1000
   gap['channels'].append(entry)
  gaps.append(gap)
 rows=[json.loads(x) for x in open(args.ledger)];clock=[x for x in rows if x['kind']=='clock_sample'];packets=[x for x in rows if x['kind']=='source_packet'];ct=np.array([x['mach_absolute_ns'] for x in clock],dtype=np.int64);heldout=[];seen=set()
 for target in np.arange(.73,len(audio)/RATE,1.0):
  wanted=first+round(float(target)*1e9);ri=int(np.argmin(abs(ct-wanted)))
  if ri in seen:continue
  seen.add(ri);row=clock[ri];qs=row['delay_query_start_steady_ns'];qe=row['delay_query_end_steady_ns'];query_host=row['mach_absolute_ns']+((qs+qe)//2-row['steady_ns']);cf=host_frame(query_host);seg=find_segment(cf);reasons=[]
  if not seg or cf/RATE-seg['span']['start']<.2 or seg['span']['end']-cf/RATE<.2:reasons.append('transition_or_quiet')
  if not row['playback_started'] or row['failed'] or row['drained'] or row['control_state'] not in ['play','resume']:reasons.append('renderer_state')
  entry={'grid_capture_seconds':float(target),'ledger_row':ri,'query_host_ns':query_host,'query_interval_ns':qe-qs,'capture_frame_from_actual_buffer_timestamp':cf,'control_state':row['control_state'],'accepted_frames':row['accepted_adddata_frames'],'delay_seconds':row['audioengine_delay_s'],'cache_seconds':row['cache_time_s'],'candidate_seconds':row['accepted_adddata_frames']/RATE-row['audioengine_delay_s'],'reasons':reasons,'channels':[]}
  if seg and seg['span']['start']+.02<cf/RATE<seg['span']['end']-.02:
   for ch in range(2):
    value=capture_match(cf,ch,seg)
    if value:entry['channels'].append(dict(value,channel=ch,error_ms=(entry['candidate_seconds']-value['source_center_sample']/RATE)*1000))
   if len(entry['channels'])!=2 or any(x['correlation']<.99 for x in entry['channels']):reasons.append('waveform_correlation_under_099')
   if len(entry['channels'])==2 and abs(entry['channels'][0]['source_center_sample']-entry['channels'][1]['source_center_sample'])>1:reasons.append('stereo_mapping_disagreement')
  else:reasons.append('no_waveform_window')
  entry['qualified']=not reasons;heldout.append(entry)
 qualified=[x for x in heldout if x['qualified']];err=[y['error_ms'] for x in qualified for y in x['channels']];viol=[x for x in qualified if any(abs(y['error_ms'])>args.threshold_ms for y in x['channels'])];packetissues=[];cursor=0
 for packet in packets:
  if packet['decoded_frame_start']!=cursor:packetissues.append({'packet':packet['packet_index'],'reason':'nonconsecutive_decoded_range'})
  cursor=packet['decoded_frame_end']
 pts_residuals=[{'packet':x['packet_index'],'residual_us':x['source_pts']-x['decoded_frame_start']/RATE*1e6} for x in packets if x.get('source_pts') is not None and abs(x['source_pts']-x['decoded_frame_start']/RATE*1e6)>.1]
 report={'kind':'independent_unique_hls_audio_clock','mapping_proven':False,'capture':str(p),'reference_pcm':str(args.reference_pcm),'reference_frames':len(ref),'captured_frames':len(audio),'timing':{'errors':sorted(set(errors)),'reported_discontinuities':m['discontinuities'],'max_frame_grid_residual_ns':max(abs(int(t)-first-int(f)*1e9/RATE) for t,f in zip(ht,fo)),'lookup':'actual per-buffer host_pts_ns plus in-buffer sample position'},'rms_spans':spans,'segments':segments,'prefix_tail':ends,'internal_quiet_gaps':gaps,'clock':{'formula':'accepted_adddata_frames/48000 - audioengine_delay_s','threshold_ms':args.threshold_ms,'no_offset_fit':True,'alignment':'Full-reference waveform anchors independent of tested clock; local waveform matching uses only those anchors.','heldout_grid':'0.73+n seconds; nearest telemetry row evaluated at delay-query midpoint, no temporal clock interpolation','qualification':'>=200ms from audio transitions, playback_started, play/resume, not failed/drained, both waveform correlations>=.99 and stereo source positions agree within1sample','qualified_samples':len(qualified),'attempted_samples':len(heldout),'error_range_ms':[min(err),max(err)] if err else None,'violation_count':len(viol),'gate_passed':bool(qualified) and not viol,'excluded_reasons':dict(collections.Counter(r for x in heldout if not x['qualified'] for r in x['reasons'])),'samples':heldout},'native_ledger':{'packets':len(packets),'decoded_frames':cursor,'range_issues':packetissues,'last_clock':clock[-1],'source_pts_residuals':pts_residuals},'limits':['Overall audio acceptance fails if any uncommanded quiet gaps exist, independent of steady clock gate.','Process-tap acquisition timing is not physical speaker audibility; GetDelay includes downstream/device latency. No fitted constant removes a domain offset.','RMS edges use5ms bins; small source-edge residuals include filter/quantization uncertainty and are not exact omitted/repeated frame counts.','Matched qualified clock intervals do not prove paused, stalled, startup, drain or general-platform clock behavior.','First/last20ms and dense40ms waveform matches establish bounded source identity; they are not bit-exact output proof.']}
 add_control_context(report,p,args.callbacks)
 q=p/'hls-audio-clock-analysis.json';q.write_text(json.dumps(report,indent=2)+'\n');summary={k:report[k] for k in ['captured_frames','timing']};summary.update(internal_quiet_gaps=len(gaps),clock={k:v for k,v in report['clock'].items() if k!='samples'},prefix_tail=ends);(p/'hls-audio-clock-summary.txt').write_text(json.dumps(summary,indent=2)+'\n');print('SUMMARY',json.dumps(summary),flush=True);print(q)
if __name__=='__main__':main()
