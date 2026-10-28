#!/usr/bin/env python3
"""Encode seeded unique stereo noise once, copy AAC into TS, verify reference.

FFmpeg/FFprobe and the repository's strict AAC-TS inspector are developer tools.
The final reference is decoded from byte-concatenated actual served TS files,
not the pre-encode synthesis. No HTTP listener is started by this command.
"""
import argparse,hashlib,json,shutil,subprocess,sys
from pathlib import Path

def run(cmd,**kw):return subprocess.run(cmd,check=True,**kw)
def sha(path):
 h=hashlib.sha256()
 with path.open('rb') as f:
  for b in iter(lambda:f.read(1024*1024),b''):h.update(b)
 return h.hexdigest()
def frames(data):
 out=[];i=0
 while i<len(data):
  h=data[i:i+7]
  if len(h)!=7 or h[0]!=255 or h[1]&0xf6!=0xf0:raise ValueError('Invalid ADTS')
  size=((h[3]&3)<<11)|(h[4]<<3)|(h[5]>>5);header=7 if h[1]&1 else 9
  if size<header or i+size>len(data) or h[6]&3:raise ValueError('Truncated/multiblock ADTS')
  out.append((i,size,hashlib.sha256(data[i+header:i+size]).hexdigest()));i+=size
 return out

def main():
 ap=argparse.ArgumentParser(description=__doc__);ap.add_argument('--root',type=Path,default=Path(__file__).parent);ap.add_argument('--repo',type=Path,default=Path('/Users/hermes/.codex/worktrees/radio-poc/kodi-donor-radio'));args=ap.parse_args();root=args.root.resolve();assets=root/'assets';assets.mkdir(parents=True,exist_ok=True)
 if (root/'metadata.json').exists():raise SystemExit('Validated fixture already exists; use a new output root to preserve evidence')
 ffmpeg=shutil.which('ffmpeg');ffprobe=shutil.which('ffprobe')
 if not(ffmpeg and ffprobe):raise SystemExit('FFmpeg and FFprobe required')
 sys.path.insert(0,str(args.repo/'plugin.audio.kodi_donor_radio/resources/lib'))
 from ad_media import extract_adts
 source=root/'encoded-once.aac';nominal=180;commands=[]
 command=[ffmpeg,'-hide_banner','-loglevel','error','-y','-f','lavfi','-i','anoisesrc=sample_rate=48000:duration=180:color=white:amplitude=0.18:seed=20261028','-f','lavfi','-i','anoisesrc=sample_rate=48000:duration=180:color=white:amplitude=0.18:seed=20261029','-filter_complex','[0:a]highpass=f=250,lowpass=f=3500,aformat=sample_fmts=fltp:channel_layouts=mono[l];[1:a]highpass=f=250,lowpass=f=3500,aformat=sample_fmts=fltp:channel_layouts=mono[r];[l][r]amerge=inputs=2[a]','-map','[a]','-c:a','aac','-profile:a','aac_low','-b:a','256k','-ar','48000','-ac','2','-flags','+bitexact','-f','adts',str(source)];commands.append(command);print('Encoding unique180s AAC once',flush=True);run(command)
 command=[ffmpeg,'-hide_banner','-loglevel','error','-y','-i',str(source),'-map','0:a:0','-c:a','copy','-f','hls','-hls_time','2','-hls_list_size','0','-hls_playlist_type','vod','-hls_segment_filename',str(assets/'segment%03d.ts'),str(assets/'reference-vod.m3u8')];commands.append(command);run(command)
 encoded=source.read_bytes();original=frames(encoded);joined=bytearray();segments=[];sample=0;first_pts=None;packetrows=[];aac_cursor=0;payload_hashes=[];tsconcat=root/'all-served-segments.ts'
 with tsconcat.open('wb') as tsout:
  for sequence,path in enumerate(sorted(assets.glob('segment*.ts'))):
   data=path.read_bytes();info,aac=extract_adts(data);packet=frames(aac);start=sample;sample+=info.samples
   if first_pts is None:first_pts=info.first_pts
   expected=first_pts+start*90000//48000
   if info.first_pts!=expected or info.last_pts!=expected+(len(packet)-1)*1920:raise ValueError('TS PTS continuity mismatch')
   if info.samples!=len(packet)*1024:raise ValueError('AAC sample count mismatch')
   segments.append({'sequence':sequence,'filename':path.name,'samples':info.samples,'start_sample':start,'end_sample':sample,'first_packet':len(packetrows),'packet_count':len(packet),'first_pts':info.first_pts,'last_pts':info.last_pts,'sha256':sha(path),'aac_sha256':hashlib.sha256(aac).hexdigest(),'bytes':len(data)})
   for offset,size,digest in packet:
    index=len(packetrows);packetrows.append({'packet':index,'segment_sequence':sequence,'reference_start_sample':index*1024,'samples':1024,'pts':first_pts+index*1920,'timebase':[1,90000],'source_adts_offset':aac_cursor+offset,'segment_adts_offset':offset,'adts_bytes':size,'aac_payload_sha256':digest});payload_hashes.append(digest)
   aac_cursor+=len(aac);joined.extend(aac);tsout.write(data)
 if len(packetrows)!=len(original) or [x[2] for x in original]!=payload_hashes:raise ValueError('Segmented AAC packets differ from original encoding')
 if len(set(payload_hashes))!=len(payload_hashes):raise ValueError('Repeated AAC packet payload in unique fixture')
 reference_aac=root/'reference-from-ts.aac';reference_aac.write_bytes(joined);reference_pcm=root/'reference-from-served-ts.f32';verify=root/'reference-adts-verification.f32'
 for inp,out in [(tsconcat,reference_pcm),(reference_aac,verify)]:
  command=[ffmpeg,'-hide_banner','-loglevel','error','-y','-i',str(inp),'-map','0:a:0','-ar','48000','-ac','2','-f','f32le',str(out)];commands.append(command);run(command)
 expectedbytes=sample*2*4
 if reference_pcm.stat().st_size!=expectedbytes or verify.stat().st_size!=expectedbytes or sha(reference_pcm)!=sha(verify):raise ValueError('TS and ADTS decoded reference mismatch')
 verify.unlink()
 with (root/'packet-map.jsonl').open('w') as f:
  for row in packetrows:f.write(json.dumps(row,sort_keys=True)+'\n')
 meta={'schema_version':1,'kind':'single-encode-unique-aac-rolling-hls','nominal_input_seconds':nominal,'sample_rate':48000,'channels':2,'seeds':[20261028,20261029],'synthesis':'Independent seeded white noise per channel,250Hz highpass/3500Hz lowpass; amplitude.18; no repeated tone loops.','aac_packets':len(packetrows),'samples':sample,'duration_seconds':sample/48000,'first_pts':first_pts,'pts_timebase':[1,90000],'samples_per_aac_packet':1024,'pts_per_aac_packet':1920,'reference_includes_aac_priming_padding':True,'initial_segment_count':1,'window_segments':6,'initial_available_samples':segments[0]['end_sample'],'endlist_after_first_get_seconds':(sample-segments[0]['end_sample'])/48000,'source_aac_sha256':sha(source),'reference_adts_sha256':sha(reference_aac),'source_and_extracted_adts_byte_identical':encoded==joined,'reference_pcm_sha256':sha(reference_pcm),'reference_pcm':str(reference_pcm),'reference_aac':str(reference_aac),'reference_all_ts':str(tsconcat),'packet_map':str(root/'packet-map.jsonl'),'segments':segments,'tools':{'ffmpeg_version':run([ffmpeg,'-version'],capture_output=True,text=True).stdout.splitlines()[0]},'commands':commands,'pdt_precision_ns':1000,'origin_clock':'Mach absolute ns paired with UTC on first media GET; exact rational sample-zero anchor persisted by server','validation':{'strict_repo_ts_inspection':True,'packet_order_identity':True,'all_packet_payloads_unique':True,'ts_decode_equals_extracted_adts_decode':True,'expected_decoded_bytes':expectedbytes}}
 meta['metadata_identity']=hashlib.sha256(json.dumps(meta,sort_keys=True).encode()).hexdigest();(root/'metadata.json').write_text(json.dumps(meta,indent=2,sort_keys=True)+'\n')
 print(json.dumps({k:meta[k] for k in ['duration_seconds','samples','aac_packets','endlist_after_first_get_seconds','reference_aac','reference_pcm','metadata_identity']},indent=2),flush=True)
if __name__=='__main__':main()
