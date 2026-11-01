#!/usr/bin/env python3
"""Fixed precomposed source proof, real loopback VAST, and retained HTTP receipts.

This is an isolated lab helper. Creative TS files contain exact subsets of the
already encoded served source; this makes no general insertion claim.
"""
import argparse
from collections import Counter
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import xml.etree.ElementTree as ET
from urllib.parse import parse_qs, urlsplit

ORIGIN = os.environ.get('KODI_DONOR_FIXTURE_ORIGIN', 'http://127.0.0.1:18789')
URL = ORIGIN+'/live/index.m3u8'
CONTRACT_SHA = '0f61940179856249077e9536643c6c2868da68853b2406abf0024849de49f1f8'
METADATA_ID = 'beb8dbb1d70f935688c04d428d361bdf90cca0fc1d7bd82ac62bfeda81b98d03'
FIRST_PACKET = '6e2b7fc4141143ff51923f0fc26127ebecfd7eb035c2d542598544973f8fd702'
PCM_SHA = '0b7f508c5534424c42d7b70231e664f208cb48bd0934d166b5949f5549c16524'
TOTAL = 8641536
PLACEMENTS = {'preroll':(0,96),'a':(768,1152),'b_paused':(1536,2304),
              'c':(2688,3072),'d':(5280,5664),'e':(7296,7680)}
EVENTS = ('impression','start','firstQuartile','midpoint','thirdQuartile','complete')
DEFAULT_CONTRACT = Path('/private/tmp/kodi-audio-validation-20261027/owned-clock-beacons-validation/acceptance-contract.json')
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / 'plugin.audio.kodi_donor_radio' / 'resources' / 'lib'))
from owned_clock import monitor_ready_payload


def canonical(value):
    return json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def file_sha(path):
    digest=hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda:stream.read(1024*1024),b''):digest.update(chunk)
    return digest.hexdigest()


def read_json(path,limit=4*1024*1024):
    raw=Path(path).read_bytes()
    if len(raw)>limit:raise ValueError('json_bound')
    def pairs(items):
        result={}
        for key,value in items:
            if key in result:raise ValueError('duplicate_json_key')
            result[key]=value
        return result
    return json.loads(raw,object_pairs_hook=pairs,parse_constant=lambda _:(_ for _ in ()).throw(ValueError('nonfinite_json')))


def load_contract(path=DEFAULT_CONTRACT):
    if file_sha(path)!=CONTRACT_SHA:raise ValueError('acceptance_contract_changed')
    return read_json(path)


def validate_ready(ready,binding):
    expected=monitor_ready_payload(binding)
    if (binding.get('player_mode')=='videoplayer_video_v1'
            and (not isinstance(ready,dict) or type(ready.get('video_queue_tenths')) is not int)) or canonical(ready)!=canonical(expected):
        raise ValueError('monitor_ready_binding')
    return True


def publish_ready(directory_fd,binding):
    from owned_clock import _rename_exclusive
    ready=monitor_ready_payload(binding)
    name='.monitor-ready.tmp'
    fd=os.open(name,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=directory_fd)
    with os.fdopen(fd,'wb') as stream:
        os.fchmod(stream.fileno(),0o600)
        stream.write(canonical(ready))
    _rename_exclusive(directory_fd,name,'monitor-ready.json')


def validate_profile(settings):
    if (settings.get('audiooutput.audiodevice') not in ('Default','DARWINOSX:default|Default')
            or settings.get('audiooutput.lowlatency') is not False
            or type(settings.get('audiooutput.channels')) is not int or settings['audiooutput.channels']!=1
            or type(settings.get('audiooutput.config')) is not int or settings['audiooutput.config']!=2
            or type(settings.get('audiooutput.processquality')) is not int or settings['audiooutput.processquality']!=30
            or settings.get('audiooutput.stereoupmix') is not False
            or settings.get('audiooutput.passthrough') is not False):
        raise ValueError('owned_requires_default_buffered_output')


def validate_proof(actual,expected):
    if (not isinstance(actual,dict) or actual.get('protocol')!=1
            or actual.get('contract_sha256')!=CONTRACT_SHA or actual!=expected):
        raise ValueError('fixture_source_proof_mismatch')


def verify_source(root):
    """Check pinned original metadata, every served asset and every raw AAC unit."""
    root=Path(root)
    metadata=read_json(root/'metadata.json')
    without=dict(metadata);without.pop('metadata_identity',None)
    identity=sha(json.dumps(without,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode())
    if identity!=METADATA_ID or metadata.get('metadata_identity')!=identity:
        raise ValueError('fixture_metadata_identity')
    if (metadata['sample_rate']!=48000 or metadata['channels']!=2 or metadata['first_pts']!=0
            or metadata['total_samples']!=TOTAL or metadata['packet_count']!=8439):
        raise ValueError('fixture_format')
    assets={}
    for item in [metadata['init_segment_info']]+metadata['segments']:
        name=item['filename'];path=root/'assets'/name
        if path.stat().st_size!=item['bytes'] or file_sha(path)!=item['sha256']:
            raise ValueError('fixture_asset_changed:'+name)
        assets[name]=item['sha256']
    if file_sha(root/metadata['reference_pcm'])!=PCM_SHA:
        raise ValueError('fixture_reference_changed')
    source=Path(metadata['source']).read_bytes()
    if sha(source)!=metadata['source_sha256']:raise ValueError('source_adts_changed')
    packets=[json.loads(line) for line in (root/'packet-map.jsonl').read_text().splitlines()]
    original=[json.loads(line) for line in Path(metadata['source_packet_map']).read_text().splitlines()]
    if len(packets)!=8439 or len(original)!=8439:raise ValueError('packet_count')
    cursor=0
    for index,(packet,adts) in enumerate(zip(packets,original)):
        length=adts['adts_bytes'];unit=source[cursor:cursor+length]
        if len(unit)<7 or unit[0]!=255 or unit[1]&0xf6!=0xf0:raise ValueError('adts_header')
        header=7 if unit[1]&1 else 9
        declared=((unit[3]&3)<<11)|(unit[4]<<3)|(unit[5]>>5)
        digest=sha(unit[header:])
        if (declared!=length or adts['source_adts_offset']!=cursor or adts['packet']!=index
                or packet['packet']!=index or packet['samples']!=1024
                or packet['reference_start_sample']!=index*1024 or packet['fmp4_pts']!=index*1024
                or packet['fmp4_dts']!=index*1024 or packet['fmp4_duration']!=1024
                or packet['aac_payload_sha256']!=digest or packet['source_adts_payload_sha256']!=digest
                or adts['aac_payload_sha256']!=digest):raise ValueError('packet_source_proof')
        cursor+=length
    if cursor!=len(source) or packets[0]['aac_payload_sha256']!=FIRST_PACKET:
        raise ValueError('source_zero_proof')
    proof=dict(protocol=1,contract_sha256=CONTRACT_SHA,metadata_sha256=file_sha(root/'metadata.json'),
               metadata_identity=identity,packet_map_sha256=file_sha(root/'packet-map.jsonl'),
               source_packet_map_sha256=file_sha(metadata['source_packet_map']),source_sha256=sha(source),
               reference_pcm_sha256=PCM_SHA,first_packet_sha256=FIRST_PACKET,total_samples=TOTAL,
               assets=assets,placements_aac_units={k:list(v) for k,v in PLACEMENTS.items()})
    return metadata,proof,source,original


def generate(root,contract=DEFAULT_CONTRACT,ffmpeg='ffmpeg',ffprobe='ffprobe'):
    from ad_media import extract_adts
    load_contract(contract)
    root=Path(root);destination=root/'owned-beacons'
    if destination.exists():raise ValueError('owned_outputs_already_exist')
    metadata,proof,source,packets=verify_source(root)
    destination.mkdir(mode=0o700)
    # Independently demux actual served fMP4, rather than trusting packet map claims.
    probe=subprocess.run([ffprobe,'-v','error','-show_packets','-show_data_hash','sha256',
                          '-select_streams','a:0','-of','json',str(root/'assets/reference-vod.m3u8')],
                         check=True,stdout=subprocess.PIPE).stdout
    observed=json.loads(probe)['packets']
    if len(observed)!=len(packets):raise ValueError('served_packet_count')
    for i,(packet,original) in enumerate(zip(observed,packets)):
        if (packet['data_hash'].lower()!='sha256:'+original['aac_payload_sha256']
                or int(packet['pts'])!=i*1024 or int(packet['duration'])!=1024):
            raise ValueError('served_packet_payload_or_pts')
    (destination/'served-packet-probe.json').write_bytes(probe)
    creatives={}
    for name,(start,end) in PLACEMENTS.items():
        begin=packets[start]['source_adts_offset']
        finish=packets[end-1]['source_adts_offset']+packets[end-1]['adts_bytes']
        raw=source[begin:finish];aac=destination/(name+'.aac');ts=destination/(name+'.ts')
        aac.write_bytes(raw)
        subprocess.run([ffmpeg,'-v','error','-nostdin','-f','aac','-i',str(aac),'-c:a','copy',
                        '-muxdelay','0','-f','mpegts',str(ts)],check=True)
        info,extracted=extract_adts(ts.read_bytes())
        if (extracted!=raw or info.samples!=(end-start)*1024 or info.sample_rate!=48000
                or info.channels!=2 or info.first_pts!=0):raise ValueError('creative_source_subset')
        creatives[name]={'start_unit':start,'end_unit':end,'aac_sha256':sha(raw),
                         'ts_sha256':file_sha(ts),'samples':info.samples}
    proof['creatives']=creatives
    proof['served_packet_probe_sha256']=file_sha(destination/'served-packet-probe.json')
    (destination/'proof.json').write_bytes(canonical(proof))
    return proof


def verify_fixture(root):
    from ad_media import extract_adts
    root=Path(root);metadata,expected,source,packets=verify_source(root)
    proof=read_json(root/'owned-beacons/proof.json')
    creatives={}
    for name,(start,end) in PLACEMENTS.items():
        raw=source[packets[start]['source_adts_offset']:packets[end-1]['source_adts_offset']+packets[end-1]['adts_bytes']]
        ts=(root/'owned-beacons'/(name+'.ts')).read_bytes();info,extracted=extract_adts(ts)
        if extracted!=raw or info.samples!=(end-start)*1024 or info.first_pts!=0:
            raise ValueError('creative_source_changed')
        if (root/'owned-beacons'/(name+'.aac')).read_bytes()!=raw:raise ValueError('creative_aac_changed')
        creatives[name]={'start_unit':start,'end_unit':end,'aac_sha256':sha(raw),
                         'ts_sha256':sha(ts),'samples':info.samples}
    expected['creatives']=creatives
    expected['served_packet_probe_sha256']=file_sha(root/'owned-beacons/served-packet-probe.json')
    # Recheck retained demux proof against exact source packets on every launch.
    observed=read_json(root/'owned-beacons/served-packet-probe.json',16*1024*1024)['packets']
    if len(observed)!=len(packets) or any(p['data_hash'].lower()!='sha256:'+q['aac_payload_sha256']
        or int(p['pts'])!=i*1024 or int(p['duration'])!=1024 for i,(p,q) in enumerate(zip(observed,packets))):
        raise ValueError('served_packet_proof_changed')
    validate_proof(proof,expected)
    return metadata,proof


def presentation_from_manifest(document, local_origin=ORIGIN):
    from ad_models import OutputSegment,Placement,Presentation,ResolvedAd,TrackingUrl
    from owned_ad_playback import canonical_timeline
    value=document['presentation'];placements=[]
    for placement in value['placements']:
        ad=dict(placement['ad'])
        ad['tracking']=tuple(TrackingUrl(**t) for t in ad['tracking'])
        ad['error_urls']=tuple(ad['error_urls'])
        placements.append(Placement(placement['id'],ResolvedAd(**ad),placement['start_sample'],placement['end_sample']))
    presentation=Presentation(tuple(OutputSegment(**s) for s in value['segments']),tuple(placements),
                              value['ended'],value['initially_finite'])
    if canonical_timeline(presentation,FIRST_PACKET,local_origin=local_origin)!=canonical(document):
        raise ValueError('canonical_timeline_changed')
    return presentation


def http_client():
    from http_client import HttpClient
    from url_policy import UrlPolicy
    return HttpClient(UrlPolicy({ORIGIN}),connect_timeout=2,read_timeout=2)


def resolve_presentation(root,nonce,client=None):
    from ad_models import OutputSegment,Placement,Presentation
    from http_client import read_bounded
    from vast import VastResolver
    metadata,proof=verify_fixture(root)
    if not re.fullmatch('[0-9a-f]{32}',nonce):raise ValueError('launch_nonce')
    client=client or http_client();cancel=threading.Event()
    with client.open(ORIGIN+'/owned-proof',{},cancel,allowed_origin=ORIGIN) as response:
        if response.status!=200:raise ValueError('server_source_proof_missing')
        validate_proof(json.loads(read_bounded(response,128*1024)),proof)
    resolver=VastResolver(client,ORIGIN);placements=[]
    for name,(start,end) in PLACEMENTS.items():
        ad=resolver.resolve(ORIGIN+'/vast/'+name+'.xml?launch='+nonce,cancel)
        if (ad is None or ad.id!=name or ad.media_url!=ORIGIN+'/creative/'+name+'/index.m3u8'
                or ad.duration_seconds!=(end-start)*1024/48000
                or [(t.event,t.url) for t in ad.tracking]!=[(event,ORIGIN+'/track/'+nonce+'/'+name+'/'+event) for event in EVENTS]
                or ad.error_urls):raise ValueError('resolved_vast_identity')
        # Fetch actual creative endpoint too and bind it to the verified local subset.
        with client.open(ad.media_url,{},cancel,allowed_origin=ORIGIN) as response:
            if response.status!=200 or read_bounded(response,4096)!=creative_playlist(name):raise ValueError('creative_hls_changed')
        with client.open(ORIGIN+'/creative/'+name+'/audio.ts',{},cancel,allowed_origin=ORIGIN) as response:
            if response.status!=200 or sha(read_bounded(response,2*1024*1024))!=proof['creatives'][name]['ts_sha256']:
                raise ValueError('served_creative_changed')
        placements.append(Placement(name,ad,start*1024,end*1024))
    segments=tuple(OutputSegment(s['sequence'],0,s['sha256'],s['start_sample'],s['samples'],None) for s in metadata['segments'])
    return Presentation(segments,tuple(placements),True,False),proof


def creative_playlist(name):
    start,end=PLACEMENTS[name];duration=(end-start)*1024/48000
    import math
    return ('#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:%d\n#EXT-X-MEDIA-SEQUENCE:0\n#EXTINF:%.3f,\naudio.ts\n#EXT-X-ENDLIST\n'%(math.ceil(duration),duration)).encode()


def vast_xml(name,nonce):
    start,end=PLACEMENTS[name];duration=(end-start)*1024/48000
    tracks=''.join('<Tracking event="%s">%s/track/%s/%s/%s</Tracking>'%(event,ORIGIN,nonce,name,event) for event in EVENTS[1:])
    return ('<VAST version="4.1"><Ad id="%s"><InLine><AdSystem>owned-fixture</AdSystem><AdTitle>%s</AdTitle>'
            '<Impression>%s/track/%s/%s/impression</Impression><Creatives><Creative><Linear>'
            '<Duration>00:00:%06.3f</Duration><TrackingEvents>%s</TrackingEvents><MediaFiles>'
            '<MediaFile delivery="streaming" type="application/vnd.apple.mpegurl">%s/creative/%s/index.m3u8</MediaFile>'
            '</MediaFiles></Linear></Creative></Creatives></InLine></Ad></VAST>'%(name,name,ORIGIN,nonce,name,duration,tracks,ORIGIN,name)).encode()


class BeaconEndpoints:
    """All attempted receipts are retained, including unknown and duplicate GETs."""
    def __init__(self,root,proof,now_ns):
        self.root=Path(root);self.proof=proof;self.now_ns=now_ns
        self.lock=threading.RLock();self.launches={};self.counts=Counter();self.rows=[]
        # A fresh server has a fresh log; never silently append a previous run.
        self.log=(self.root/'beacon-receipts.jsonl').open('x')

    def get(self,path,head=False,receipt_ns=None):
        receipt_ns=self.now_ns() if receipt_ns is None else receipt_ns
        parts=urlsplit(path);route=parts.path
        with self.lock:
            if route=='/owned-proof' and not parts.query:return 200,'application/json',canonical(self.proof)
            match=re.fullmatch('/vast/([a-z_]+)\\.xml',route)
            if match and match[1] in PLACEMENTS:
                query=parse_qs(parts.query)
                nonce=query.get('launch',[''])[0]
                if set(query)!={'launch'} or len(query['launch'])!=1 or not re.fullmatch('[0-9a-f]{32}',nonce):
                    return 400,'text/plain',b'Invalid launch'
                if nonce not in self.launches and len(self.launches)>=16:return 409,'text/plain',b'Launch bound'
                if not head:self.launches.setdefault(nonce,set()).add(match[1])
                return 200,'application/xml',vast_xml(match[1],nonce)
            match=re.fullmatch('/creative/([a-z_]+)/(index.m3u8|audio.ts)',route)
            if match and match[1] in PLACEMENTS and not parts.query:
                name=match[1]
                if match[2]=='index.m3u8':return 200,'application/vnd.apple.mpegurl',creative_playlist(name)
                body=(self.root/'owned-beacons'/(name+'.ts')).read_bytes()
                if sha(body)!=self.proof['creatives'][name]['ts_sha256']:raise ValueError('served_creative_changed')
                return 200,'video/mp2t',body
            if route.startswith('/track/'):
                if len(self.rows)>=4096:raise RuntimeError('receipt_bound_exhausted')
                match=re.fullmatch('/track/([0-9a-f]{32})/([a-z_]{1,32})/([A-Za-z]{1,32})',route)
                nonce,name,event=match.groups() if match else ('','','')
                key=(nonce,name,event);known=bool(match and not parts.query and name in PLACEMENTS and event in EVENTS
                                                and self.launches.get(nonce)==set(PLACEMENTS))
                duplicate=self.counts[key]>0 if known else False
                accepted=known and not duplicate and not head and len(self.rows)<4096
                status=204 if accepted else 409 if duplicate else 400
                row=dict(kind='owned_http_receipt',session_id=sha(nonce.encode())[:16] if nonce else '',creative=name,event=event,
                         known_launch=self.launches.get(nonce)==set(PLACEMENTS),receipt_bound_exhausted=len(self.rows)==4095,
                         receipt_mach_ns=receipt_ns,accepted=accepted,duplicate=duplicate,status=status,
                         method='HEAD' if head else 'GET',attempt=len(self.rows)+1,path_sha256=sha(path.encode()))
                self.rows.append(row)
                if accepted:self.counts[key]+=1
                self.log.write(json.dumps(row,sort_keys=True)+'\n');self.log.flush()
                return status,'text/plain',b'' if accepted else b'Rejected tracking attempt'
            return 404,'text/plain',b'Not found'

    def stats(self):
        with self.lock:return {'attempts':len(self.rows),'accepted':sum(r['accepted'] for r in self.rows),
                              'rejected':sum(not r['accepted'] for r in self.rows),'receipt_ledger':list(self.rows)}

    def close(self):self.log.close()


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,required=True);parser.add_argument('--repo',type=Path,required=True)
    parser.add_argument('--contract',type=Path,default=DEFAULT_CONTRACT)
    args=parser.parse_args();sys.path.insert(0,str(args.repo/'plugin.audio.kodi_donor_radio/resources/lib'))
    print(json.dumps(generate(args.root,args.contract),indent=2))


def _hal_native_manifest(library):
    """Return the owner-supplied, non-symlinked native candidate manifest.

    The Python freeze deliberately does not authorize a clock candidate by
    itself: a launch must name the exact installed dylib and addon manifest
    that a local builder measured.  Keeping that hand-off explicit prevents a
    source-only digest from accidentally authorizing a different installed
    inputstream binary.
    """
    configured=os.environ.get('KODI_DONOR_HAL_NATIVE_MANIFEST')
    path=Path(configured) if configured else Path(library)/'owned-hal-native-manifest.json'
    if not path.is_absolute() or path.is_symlink():raise ValueError('hal_native_manifest_path')
    try:stat=path.stat()
    except OSError as exc:raise ValueError('hal_native_manifest_missing') from exc
    if not path.is_file() or stat.st_uid!=os.getuid() or stat.st_mode&0o022:raise ValueError('hal_native_manifest_trust')
    return path,read_json(path)


def _manifest_file(manifest,key):
    entry=manifest.get(key)
    if not isinstance(entry,dict):raise ValueError('hal_native_manifest_schema')
    raw_path=entry.get('path');digest=entry.get('sha256')
    if not isinstance(raw_path,str) or not Path(raw_path).is_absolute() or not re.fullmatch(r'[0-9a-f]{64}',str(digest)):
        raise ValueError('hal_native_manifest_schema')
    path=Path(raw_path)
    if path.is_symlink() or not path.is_file() or path.stat().st_uid!=os.getuid() or path.stat().st_mode&0o022:
        raise ValueError('hal_native_artifact_trust')
    if file_sha(path)!=digest:raise ValueError('hal_native_artifact_hash')
    return path


def load_hal_clock(library, installed_addon):
    """Verify the freeze and the exact installed native candidate at activation."""
    if sys.platform != 'darwin':
        raise ValueError('hal_native_platform_unsupported')
    try:
        addon_root=Path(installed_addon).resolve(strict=True)
    except (OSError,TypeError,ValueError) as exc:
        raise ValueError('hal_native_addon_path') from exc
    if not addon_root.is_dir():raise ValueError('hal_native_addon_path')
    directory=Path(__file__).resolve().parent/'hal-clock-v1'
    contract=json.loads((directory/'contract.json').read_bytes())
    freeze=json.loads((directory/'freeze.json').read_bytes())
    if contract.get('schema')!='owned-hal-clock-v1' or freeze.get('schema')!='owned-hal-clock-v1':
        raise ValueError('hal_contract_schema')
    if sha((directory/'contract.json').read_bytes())!=freeze['contract_sha256']:
        raise ValueError('hal_contract_hash')
    if sha((Path(library)/'owned_hal_clock.py').read_bytes())!=freeze['provider_sha256']:
        raise ValueError('hal_provider_hash')
    if sha((Path(library)/'owned_hal_authorization.py').read_bytes())!=freeze['authorization_provider_sha256']:
        raise ValueError('hal_authorization_provider_hash')
    _,manifest=_hal_native_manifest(library)
    if manifest.get('schema')!='owned-hal-native-manifest-v1':raise ValueError('hal_native_manifest_schema')
    source=manifest.get('native_source_sha256')
    if not isinstance(source,dict):raise ValueError('hal_native_manifest_schema')
    required={'native_clock_sha256':'src/OwnedHalClock.h',
              'native_authorization_sha256':'src/OwnedHalAuthorization.h',
              'native_transport_sha256':'src/OwnedClockTransport.h'}
    if any(source.get(path)!=freeze.get(field) for field,path in required.items()):
        raise ValueError('hal_native_source_binding')
    dylib=_manifest_file(manifest,'dylib').resolve()
    addon_manifest=_manifest_file(manifest,'addon_manifest').resolve()
    if addon_manifest!=addon_root/'addon.xml':raise ValueError('hal_native_artifact_not_installed')
    try:
        addon=ET.parse(addon_manifest).getroot()
    except (OSError,ET.ParseError) as exc:
        raise ValueError('hal_native_addon_manifest') from exc
    extension=addon.find("./extension[@point='kodi.inputstream']")
    library_name=extension.get('library_osx') if extension is not None else None
    if (addon.get('id')!='inputstream.ffmpegdirect' or not isinstance(library_name,str)
            or not library_name or Path(library_name).name!=library_name
            or dylib!=addon_root/library_name):
        raise ValueError('hal_native_artifact_not_installed')
    return freeze
