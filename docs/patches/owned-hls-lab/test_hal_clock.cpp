#if __has_include("OwnedHalClock.h")
#include "OwnedHalClock.h"
#else
#include <cstdio>
int main(){std::fputs("FAIL: historical HAL clock implementation absent\n",stderr);return 1;}
#endif
#ifdef OWNED_HAL_CLOCK_V1
#include <cstdio>
#include <cstdlib>
#include <memory>
using namespace owned_hal;
#define CHECK(x) do{if(!(x)){std::fprintf(stderr,"FAIL line %d: %s\n",__LINE__,#x);std::exit(1);}}while(0)
static HalSegment segment(uint64_t i,bool source=false) {
 HalSegment s{};s.sequence=i+1;s.binding.nonce[0]=1;s.device_generation=s.stream_generation=1;
 s.playback_revision=s.playback_epoch=2;s.gain_revision=s.gain_epoch=1;s.control_acquired=true;
 s.in_flight_generation=s.native_gate=s.authorized_native_epoch=1;s.resume_playback_revision=2;
 s.publication_sequence=s.applied_intent=s.desired_intent=2;s.playing=s.requested_playing=s.resume_granted=true;
 s.applied_gain=1;s.output_sample_time=i*512;s.output_ticks=1000000000+i*32000000/3;
 s.entry_ticks=s.now_ticks=s.output_ticks-1000000;s.exit_ticks=s.entry_ticks;
 s.output_flags=s.now_flags=3;s.timebase_numer=s.timebase_denom=1;s.output_host_ns=s.output_ticks;
 s.applied.binding=s.binding;s.applied.device_generation=s.applied.stream_generation=1;
 s.applied.playback_revision=s.playback_revision;s.applied.playback_epoch=s.playback_epoch;
 s.applied.gain_revision=s.gain_revision;s.applied.gain_epoch=s.gain_epoch;
 s.applied.effective_intent=2;s.applied.started=s.applied.sample_zero_proven=s.applied.playing=1;
 s.applied.native_fence=1;s.applied.resume_playback_revision=2;s.applied.volume=100;s.applied.supplied_gain=1;
 s.applied.request_start_ns=s.entry_ticks-10000000;s.applied.request_end_ns=s.applied.issue_ns=s.applied.request_start_ns;
 s.applied.expiry_ns=s.applied.request_start_ns+500000000;
 if(source){s.source_start=(i-18)*512;s.source_count=s.consumed_frames=s.copied_frames=512;s.zero_frames=0;}
 ScheduledEndNs(s.output_ticks,1,1,s.copied_frames,s.source_end_ns);return s;
}
static void observe(HalClock& c,AppliedControlView& v,const HalSegment& s){CHECK(v.Observe(s));CHECK(c.Observe(s)==Status::ok);}
static void startup(HalClock& c,AppliedControlView& v) {
 for(unsigned i=0;i<18;++i){auto s=segment(i);observe(c,v,s);if(i%2){auto q=c.Query(s.output_ticks,s.output_ticks,v,1,2);CHECK(!q.qualified);}}
 CHECK(c.Ready());CHECK(c.Release());
}
int main(){
 auto core=std::make_unique<HalCore>();core->RequireStartupReadiness();CHECK(!core->StartupReleased());core->ReleaseStartup();CHECK(core->StartupReleased());
 // Actual delivery gate: readiness observes real zero callbacks without consuming PCM.
 core=std::make_unique<HalCore>();core->RequireStartupReadiness();CHECK(core->BeginEffectiveIntent(2)==Status::ok);
 float pcm[4096]={},output[1024]={};CHECK(core->source.Push(pcm,2048));HalClock real;AppliedControlView rv;
 for(unsigned i=0;i<21;++i){auto model=segment(i);auto control=model.applied;control.gain_revision=i+1;
 CHECK(core->Publish(control)==Status::ok);CallbackInput in{};in.output=output;in.output_bytes=4096;in.channels=2;in.buffers=1;
 in.output_sample_time=model.output_sample_time;in.output_ticks=model.output_ticks;in.now_ticks=model.now_ticks;
 in.entry_ticks=model.entry_ticks;in.exit_ticks=model.exit_ticks;in.output_flags=in.now_flags=3;in.timebase_numer=in.timebase_denom=1;
 auto result=core->Render(in);CHECK(result.reason==Reason::none);CHECK(result.consumed_frames==(i<18?0:512));
 HalSegment acquired{};CHECK(core->evidence.Pop(acquired));observe(real,rv,acquired);
 if(i<18&&i%2){auto read=real.Query(in.output_ticks,in.output_ticks,rv,1,2);CHECK(!read.qualified);}
 if(i==17){CHECK(real.Ready());CHECK(real.Release());core->ReleaseStartup();}}
 CHECK(core->source.Consumed()==1536);
 HalClock c;AppliedControlView v;startup(c,v);
 for(unsigned i=18;i<=20;++i)observe(c,v,segment(i,true));
 const auto s=segment(20,true);const auto before=c.Query(s.output_ticks,s.output_ticks,v,1,2);CHECK(!before.qualified); // frame1024 strict
 auto q=c.Query(s.output_ticks+20834,s.output_ticks+20834,v,1,2);CHECK(q.qualified&&q.source_frame==1025);
 CHECK(q.source_zero_sequence==19&&q.deadline_ns==segment(18,true).output_ticks+120000000);
 CHECK(!c.Query(s.output_ticks+20834,s.output_ticks+5020835,v,1,2).qualified);
 CHECK(!c.Query(s.output_ticks+20834,s.output_ticks+20834,v,1,3).qualified);
 // Pending intent revokes; a new actual effective epoch requires all nine queries.
 HalClock late;AppliedControlView lv;startup(late,lv);
 for(unsigned i=18;i<40;++i)observe(late,lv,segment(i,true));
 CHECK(!late.Query(segment(39,true).output_ticks,segment(39,true).output_ticks,lv,1,2).qualified);
 CHECK(late.InitialFailed());
 for(int fault=0;fault<7;++fault){HalClock bad;auto s=segment(0);if(fault==0)s.output_flags=0;if(fault==1)s.output_sample_time=.5;
 if(fault==2)s.output_sample_time=9007199254740992.;if(fault==3)s.timebase_numer=0;
 if(fault==4){s.output_ticks=UINT64_MAX;s.timebase_numer=UINT32_MAX;}
 if(fault==5)s.entry_ticks=s.output_ticks+1;if(fault==6)s.exit_ticks=s.entry_ticks-1;
 CHECK(bad.Observe(s)==Status::invalid);CHECK(bad.Observe(segment(0))==Status::retired);}
 for(int delta:{-1000,1000}){HalClock gap;AppliedControlView gv;startup(gap,gv);
 for(unsigned i=18;i<=20;++i)observe(gap,gv,segment(i,true));auto next=segment(21,true);next.output_ticks+=delta;next.output_host_ns=next.output_ticks;ScheduledEndNs(next.output_ticks,1,1,512,next.source_end_ns);observe(gap,gv,next);
 uint64_t edge=segment(21,true).output_ticks+(delta<0?-500:500);CHECK(!gap.Query(edge,edge,gv,1,2).qualified);}
 // Genuine gain change reacquires on nine actual queries over >=120ms; renewal does not reset.
 HalClock resume;AppliedControlView resumable;startup(resume,resumable);
 for(unsigned i=18;i<=20;++i)observe(resume,resumable,segment(i,true));
 CHECK(resume.Query(s.output_ticks+30000,s.output_ticks+30000,resumable,1,2).qualified);
 for(unsigned i=21;i<=37;++i){auto changed=segment(i,true);changed.gain_epoch=changed.applied.gain_epoch=2;
 changed.applied_intent=changed.desired_intent=changed.applied.effective_intent=3;
 changed.gain_revision=changed.applied.gain_revision=i;changed.publication_sequence=i;
 observe(resume,resumable,changed);if(i%2){auto result=resume.Query(changed.output_ticks+30000,changed.output_ticks+30000,resumable,1,3);
 CHECK(result.qualified==(i==37));}}
 // Current lease is derived only from drained acquired records, not a new desired offer.
 HalClock lease;AppliedControlView av;startup(lease,av);for(unsigned i=18;i<=20;++i)observe(lease,av,segment(i,true));
 auto old=*av.Last();old.sequence++;old.applied.expiry_ns=1;AppliedControlView stale;CHECK(stale.Observe(old));
 CHECK(!lease.Query(s.output_ticks+20834,s.output_ticks+20834,stale,1,2).qualified);
 HalClock bounded;for(uint64_t i=0;i<80000;++i)CHECK(bounded.Observe(segment(i))==Status::ok);
 CHECK(bounded.Observe(segment(80000))==Status::invalid);
 std::puts("HAL clock boundaries/startup/overflow/coverage passed");
}
#endif
