# SPDX-License-Identifier: GPL-2.0-or-later
"""Real VAST tracking for one complete, immutable owned controlled fixture."""
from dataclasses import asdict, dataclass
import hashlib
import json
import math
from urllib.parse import urlsplit

from ad_models import OutputSegment, Placement, PlaybackSample, Presentation, ResolvedAd, TrackingUrl
from ad_tracking import AdTracker, BeaconWorker
from owned_clock import URL, TOTAL_SAMPLES, _hex

EVENTS = frozenset(('impression','start','firstQuartile','midpoint','thirdQuartile','complete'))


def _local_url(value):
    if not isinstance(value,str) or len(value)>8192 or any(ord(c)<33 or ord(c)==127 for c in value):
        return False
    try:
        parts = urlsplit(value)
        return (parts.scheme=='http' and parts.hostname=='127.0.0.1' and parts.port==18789
                and parts.username is None and parts.password is None and not parts.fragment)
    except ValueError:
        return False


def _text(value):
    return isinstance(value,str) and 0<len(value)<=256 and all(ord(c)>=32 for c in value)


def _validate(presentation):
    if (type(presentation) is not Presentation or type(presentation.segments) is not tuple
            or type(presentation.placements) is not tuple or not 1<=len(presentation.segments)<=128
            or not 1<=len(presentation.placements)<=8 or type(presentation.ended) is not bool
            or type(presentation.initially_finite) is not bool):
        raise ValueError('presentation_bound_or_mutability')
    end = 0
    placement_ids = {p.id for p in presentation.placements if type(p) is Placement and _text(p.id)}
    if len(placement_ids)!=len(presentation.placements):
        raise ValueError('placement_identity')
    for sequence, segment in enumerate(presentation.segments):
        if (type(segment) is not OutputSegment or type(segment.sequence) is not int or segment.sequence!=sequence
                or type(segment.discontinuity) is not int or segment.discontinuity!=0
                or not _hex(segment.resource_id,64) or type(segment.start_sample) is not int or segment.start_sample!=end
                or type(segment.samples) is not int or segment.samples<=0 or segment.samples%1024
                or (segment.placement_id is not None and segment.placement_id not in placement_ids)):
            raise ValueError('segment_source_identity')
        end += segment.samples
    if end!=TOTAL_SAMPLES:
        raise ValueError('complete_source_required')
    previous_end = 0
    for placement in presentation.placements:
        ad = placement.ad
        if (type(placement.start_sample) is not int or type(placement.end_sample) is not int
                or not previous_end<=placement.start_sample<placement.end_sample<=TOTAL_SAMPLES
                or placement.start_sample%1024 or placement.end_sample%1024 or type(ad) is not ResolvedAd
                or not _text(ad.id) or not _local_url(ad.media_url)
                or type(ad.duration_seconds) not in (int,float) or not math.isfinite(ad.duration_seconds)
                or abs(ad.duration_seconds-(placement.end_sample-placement.start_sample)/48000)>1/48000
                or type(ad.tracking) is not tuple or not 1<=len(ad.tracking)<=32
                or type(ad.error_urls) is not tuple or len(ad.error_urls)>8):
            raise ValueError('placement_source_identity')
        if (any(type(record) is not TrackingUrl or record.event not in EVENTS or not _local_url(record.url)
                for record in ad.tracking) or any(not _local_url(url) for url in ad.error_urls)):
            raise ValueError('tracking_origin_or_bound')
        previous_end = placement.end_sample


def canonical_timeline(presentation, first_packet_sha256):
    """Canonical UTF-8 JSON bytes; Task3 supplies verified asset hashes as resource_id.

    Hashes bind declared source assets and all tracking metadata. This function
    does not claim to read those assets or verify their AAC packet contents.
    """
    _validate(presentation)
    if not _hex(first_packet_sha256,64):
        raise ValueError('first_packet_identity')
    document = dict(protocol=1,qualification_scope='controlled_fixture',rate=48000,
                    source_origin=0,total_samples=TOTAL_SAMPLES,selected_url=URL,
                    first_packet_sha256=first_packet_sha256,presentation=asdict(presentation))
    return json.dumps(document,sort_keys=True,separators=(',',':'),ensure_ascii=True,allow_nan=False).encode('utf-8')


def timeline_sha256(presentation, first_packet_sha256):
    return hashlib.sha256(canonical_timeline(presentation,first_packet_sha256)).hexdigest()


def _opaque(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()[:16]


@dataclass(frozen=True)
class HalEventIntent:
    placement_id: str
    event: str
    url: str
    event_digest: str
    decision_id: str
    decision_ns: int
    evidence: object
    tracker_proof: object
    initial: bool


class OwnedAdPlayback:
    """Callback-safe tracker bridge; only the worker performs network IO.

    Call control() from player callbacks, then flush the provider's queued
    commands in the monitor loop. Feed every returned transport snapshot to
    observe(), in order. Neither method reads transport files.
    """
    def __init__(self, binding, provider, presentation, client, cancel, emit):
        if (binding.selected_url!=URL or provider.binding.nonce!=binding.nonce
                or provider.binding.timeline_sha256!=binding.timeline_sha256
                or provider.binding.first_packet_sha256!=binding.first_packet_sha256
                or provider.binding.launch_mach_ns!=binding.launch_mach_ns
                or provider.binding.player_mode!=binding.player_mode
                or provider.binding.requested_core!=binding.requested_core
                or provider.binding.video_queue_tenths!=binding.video_queue_tenths
                or provider.binding.video_extradata_hex!=binding.video_extradata_hex
                or provider.binding.output_backend!=binding.output_backend
                or provider.binding.hal_phase!=binding.hal_phase
                or provider.binding.hal_clock_version!=binding.hal_clock_version
                or provider.binding.native_pid!=binding.native_pid
                or provider.binding.selected_url!=binding.selected_url
                or (binding.incarnation is not None and provider.binding.incarnation!=binding.incarnation)
                or timeline_sha256(presentation,binding.first_packet_sha256)!=binding.timeline_sha256):
            raise ValueError('owned_presentation_binding')
        self.binding,self.provider,self.presentation = binding,provider,presentation
        self.cancel,self.emit = cancel,emit
        self.session_id = _opaque(binding.nonce)
        self.worker = BeaconWorker(client,cancel,URL)
        self.tracker = AdTracker(self.session_id,self._tracker_event,self._enqueue,allow_preroll_start=True)
        self._hal=binding.output_backend=='hal_callback_v1'
        self._attempted=0;self._event_intents=[]
        self.before_hal_probe=lambda:None
        self._closed = False
        self._consumed = 0
        self._evidence = None
        self._emissions = []
        self._had_valid = False
        self._interrupted = False

    def _details(self, evidence):
        return dict(session_id=self.session_id,row_sequence=evidence.row_sequence,
                    row_sha256=evidence.row_sha256,generation=evidence.generation,
                    native_generation=evidence.native_generation,query_host_ns=evidence.query_host_ns,
                    mapped_seconds=evidence.seconds,**{k:getattr(evidence,k) for k in (
                        'backend','query_start_ns','query_end_ns','segment_sha256','applied_sequence','applied_sha256',
                        'effective_intent','playback_epoch','gain_epoch','device_generation','stream_generation',
                        'source_zero_sequence','source_zero_sha256','source_zero_ticks','timebase_numer','timebase_denom','initial_deadline_ns')})

    def _tracker_event(self, event):
        if self._hal and event.get('reason')=='queued':event=dict(event,reason='pending_native_queue_authorization')
        self._emissions.append(dict(event,decision_ns=self._decision_ns,**{k:v for k,v in self._details(self._evidence).items() if k!='session_id'}))

    def _enqueue(self, placement_id, event, url):
        if self._hal:
            if len(self._event_intents)>=32:return False
            digest=hashlib.sha256(json.dumps([self.session_id,placement_id,event,hashlib.sha256(url.encode()).hexdigest(),
                self._evidence.row_sha256,self._decision_id],separators=(',',':')).encode()).hexdigest()
            placement=next(p for p in self.presentation.placements if p.id==placement_id)
            self._event_intents.append(HalEventIntent(placement_id,event,url,digest,self._decision_id,self._decision_ns,
                self._evidence,self._decision_proof,placement.start_sample==0 and event in ('impression','start')))
            return True # staged only; _tracker_event explicitly labels it pending
        evidence = self._evidence
        decision_ns = self._decision_ns
        placement = next(p for p in self.presentation.placements if p.id==placement_id)
        fraction = {'impression':0,'start':0,'firstQuartile':.25,'midpoint':.5,'thirdQuartile':.75,'complete':1}[event]
        threshold = (placement.start_sample+(placement.end_sample-placement.start_sample)*fraction)/48000
        def authorize_delivery():
            checked = []
            def approve(current):
                checked.append(current.authorization_ns)
                return not self._closed and not self.cancel.is_set()
            authorized = bool(self.provider.authorize(evidence,approve))
            # This user-supplied evidence sink is outside all provider/worker locks.
            self.emit(dict(kind='owned_beacon_authorization',event=event,
                           placement_id=_opaque(placement_id),authorized=authorized,
                           threshold_seconds=threshold,decision_ns=decision_ns,
                           authorization_ns=checked[0] if checked else self.provider.now_ns(),
                           **self._details(evidence)))
            return authorized
        return self.provider.authorize(evidence,lambda current:self.worker.enqueue(
            placement_id,event,url,authorize=authorize_delivery))

    def observe(self, evidence=None):
        if self._hal:return self._observe_hal(evidence)
        evidence = self.provider.snapshot() if evidence is None else evidence
        emissions = []
        with self.provider.lock:
            if self.provider.retired or self.cancel.is_set():
                self._stop_tracking(self.provider.reason if self.provider.retired else 'cancelled')
            if self._closed:
                return False
            if not evidence.valid:
                if self._had_valid and not self._interrupted:
                    self.tracker.interrupt()
                    self._interrupted = True
                return False
            if evidence.row_sequence<=self._consumed:
                return False
            def decide(current):
                if self.cancel.is_set():
                    return False
                self._consumed = evidence.row_sequence
                self._evidence = evidence
                self._decision_ns = current.authorization_ns
                self._had_valid = True
                self._interrupted = False
                sample = PlaybackSample(self.session_id,evidence.seconds,evidence.query_host_ns/1e9,
                                        True,False,False,True)
                self.tracker.observe(self.presentation,sample)
                emissions.extend(self._emissions)
                self._emissions.clear()
                return True
            accepted = bool(self.provider.authorize(evidence,decide))
        for event in emissions:
            self.emit(event)
        return accepted

    def _hal_drop(self):
        # A denied observation or event is terminal for continuity and dedup.
        with self.provider.lock:
            self.tracker.interrupt();self._interrupted=True

    def _observe_hal(self,evidence=None):
        evidence=self.provider.snapshot() if evidence is None else evidence
        with self.provider.lock:
            if self._closed or self.cancel.is_set() or self.provider.retired:return False
            if not evidence.valid:
                if self._had_valid:self.tracker.interrupt();self._interrupted=True
                return False
            if evidence.row_sequence<=self._attempted:return False
            self._attempted=evidence.row_sequence
        self.before_hal_probe()
        decision_id=hashlib.sha256(('%s:%s:%d'%(self.session_id,evidence.row_sha256,evidence.generation)).encode()).hexdigest()
        proof=self.provider.probe(evidence,'tracker',initial=not self._had_valid,decision_id=decision_id)
        staged=[];emissions=[]
        def decide(current):
            if self._closed or self.cancel.is_set():return False
            self._consumed=evidence.row_sequence;self._evidence=evidence;self._decision_ns=current.authorization_ns
            self._decision_id=decision_id;self._decision_proof=proof;self._event_intents=[]
            self._had_valid=True;self._interrupted=False
            sample=PlaybackSample(self.session_id,evidence.seconds,evidence.query_host_ns/1e9,True,False,False,True)
            self.tracker.observe(self.presentation,sample)
            staged.extend(self._event_intents);self._event_intents=[]
            emissions.extend(self._emissions);self._emissions.clear();return True
        accepted=bool(self.provider.consume_proof(evidence,proof,'tracker',decide))
        self.emit(dict(kind='owned_hal_tracker_authorization',authorized=accepted,decision_id=decision_id,
            proof_id=proof.request['request_id'] if proof else None,proof_sha256=proof.response_sha256 if proof else None,
            checked_mach_ns=self.provider.now_ns(),**self._details(evidence)))
        if not accepted:self._hal_drop();return False
        outcomes={}
        for intent in staged:
            self.before_hal_probe() # due gain/control work outranks another exchange
            queued=self._queue_hal(intent)
            key=(_opaque(intent.placement_id),intent.event)
            outcomes[key]=outcomes.get(key,False) or queued
        for event in emissions:
            if event['reason']=='pending_native_queue_authorization':
                event['reason']='queued' if outcomes.get((event['placement_id'],event['event'])) else 'native_queue_rejected'
            event.update(decision_id=decision_id,tracker_proof_id=proof.request['request_id'],tracker_proof_sha256=proof.response_sha256)
            self.emit(event)
        return True

    def _queue_hal(self,intent):
        evidence=intent.evidence
        proof=self.provider.probe(evidence,'queue',initial=intent.initial,decision_id=intent.decision_id,
            event_digest=intent.event_digest,parent=intent.tracker_proof,decision_ns=intent.decision_ns)
        context={}
        def dispatch():
            dispatch_proof=self.provider.probe(evidence,'dispatch',initial=intent.initial,decision_id=intent.decision_id,
                event_digest=intent.event_digest,parent=proof,decision_ns=intent.decision_ns,queue_ns=context['queue_ns'])
            checked=[]
            def approve(current):
                checked.append(current.authorization_ns);return not self._closed and not self.cancel.is_set()
            authorized=bool(self.provider.consume_proof(evidence,dispatch_proof,'dispatch',approve))
            self._hal_event_authorization(intent,authorized,checked[0] if checked else self.provider.now_ns(),dispatch_proof,context['queue_ns'],'dispatch')
            if not authorized:self._hal_drop()
            return authorized
        def queue(current):
            if self._closed or self.cancel.is_set():return False
            context['queue_ns']=current.authorization_ns
            return self.worker.enqueue(intent.placement_id,intent.event,intent.url,authorize=dispatch)
        queued=bool(self.provider.consume_proof(evidence,proof,'queue',queue))
        self.emit(dict(kind='owned_beacon_queue',event=intent.event,placement_id=_opaque(intent.placement_id),queued=queued,
            decision_id=intent.decision_id,event_digest=intent.event_digest,decision_ns=intent.decision_ns,
            queue_ns=context.get('queue_ns'),proof_id=proof.request['request_id'] if proof else None,
            proof_sha256=proof.response_sha256 if proof else None,**self._details(evidence)))
        if not queued:
            self._hal_event_authorization(intent,False,self.provider.now_ns(),proof,context.get('queue_ns'),'queue')
            self._hal_drop()
        return queued

    def _hal_event_authorization(self,intent,authorized,checked,proof,queue_ns,stage):
        placement=next(p for p in self.presentation.placements if p.id==intent.placement_id)
        fraction={'impression':0,'start':0,'firstQuartile':.25,'midpoint':.5,'thirdQuartile':.75,'complete':1}[intent.event]
        threshold=(placement.start_sample+(placement.end_sample-placement.start_sample)*fraction)/48000
        self.emit(dict(kind='owned_beacon_authorization',event=intent.event,placement_id=_opaque(intent.placement_id),
            authorized=authorized,threshold_seconds=threshold,decision_ns=intent.decision_ns,authorization_ns=checked,
            decision_id=intent.decision_id,event_digest=intent.event_digest,queue_ns=queue_ns,authorization_stage=stage,
            proof_id=proof.request['request_id'] if proof else None,proof_sha256=proof.response_sha256 if proof else None,
            **self._details(intent.evidence)))

    def control(self, state, issued_ns=None):
        with self.provider.lock:
            command = self.provider.control(state,issued_ns)
            if state in ('pause','stop') or self.provider.retired:
                self.tracker.interrupt()
                self._interrupted = True
            if self.provider.retired:
                self._stop_tracking(self.provider.reason)
            return command

    def route(self, selected_url):
        self.provider.route(selected_url)
        if self.provider.retired:
            self.retire('route_mismatch')

    def _stop_tracking(self, reason):
        self._closed = True
        self.provider.retire(reason)
        self.tracker.interrupt()
        self.tracker.finish(reason)
        self.worker.close(wait=False)

    def retire(self, reason):
        """Error/seek/route callbacks retire without waiting for network cleanup."""
        with self.provider.lock:
            self._stop_tracking(reason)

    def close(self, reason='closed'):
        """Monitor cleanup: cancel immediately, then join outside callback lock."""
        with self.provider.lock:
            self._stop_tracking(reason)
        self.worker.close()
