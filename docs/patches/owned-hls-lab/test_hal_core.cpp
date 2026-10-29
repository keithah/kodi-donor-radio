// Each check exercises source ordering or a fail-closed output boundary.
#include "OwnedHalCore.h"
#include <cstdio>
#include <cstdlib>
#include <memory>
#include <limits>
#include <thread>
#include <pthread.h>
#include <unistd.h>
#include <sys/wait.h>
#include <signal.h>
extern "C" void hal_rt_enable(bool);
using namespace owned_hal;
#define CHECK(x) do { if (!(x)) { std::fprintf(stderr,"FAIL %s:%d %s\n",__FILE__,__LINE__,#x); std::exit(1); } } while(0)
static EffectiveControl control() {
  EffectiveControl c{}; c.binding.nonce[0]=1; c.binding.incarnation[0]=2;
  c.binding.timeline[0]=3; c.device_generation=1; c.stream_generation=1;
  c.playback_revision=1; c.playback_epoch=1; c.gain_revision=1; c.gain_epoch=1;
  c.request_start_ns=900000000; c.request_end_ns=910000000;
  c.issue_ns=910000000; c.expiry_ns=1400000000; c.volume=100; c.supplied_gain=1;
  c.started=1; c.sample_zero_proven=1; c.playing=1; return c;
}
static CallbackInput input(float* p, uint64_t index=0) {
  CallbackInput i{}; i.output=p; i.output_bytes=4096; i.channels=2; i.buffers=1;
  i.output_sample_time=double(index*512); i.output_ticks=1000000000+index*32000000/3;
  i.now_ticks=i.output_ticks-1000000; i.entry_ticks=i.now_ticks; i.exit_ticks=i.entry_ticks;
  i.output_flags=3; i.now_flags=3; i.timebase_numer=1; i.timebase_denom=1; return i;
}
static void fill(float* p, uint32_t frames, uint64_t start=0) {
  for(uint32_t n=0;n<frames;++n) {p[2*n]=float(start+n+1);p[2*n+1]=-float(start+n+1);}
}
struct NotLockFree : std::atomic<uint64_t> { explicit NotLockFree(uint64_t v=0):std::atomic<uint64_t>(v) {} bool is_lock_free() const noexcept {return false;} };
static HalCore* boundary_core=nullptr;
static EffectiveControl boundary_control{};
static bool boundary_route=false;
static uint64_t change_after_acquisition() noexcept {
 if(boundary_route)boundary_core->Retire(Reason::route);
 else CHECK(boundary_core->Publish(boundary_control)==Status::ok);
 return 999000001;
}
static CallbackResult render(HalCore& core,const CallbackInput& in) {
 hal_rt_enable(true);auto result=core.Render(in);hal_rt_enable(false);return result;
}
int main() {
  // If interposition stops working, these children return zero and fail the test.
  for(int operation=0;operation<3;++operation){pid_t child=fork();CHECK(child>=0);
    if(child==0){hal_rt_enable(true);if(operation==0){void* volatile p=malloc(4);(void)p;}
      if(operation==1){pthread_mutex_t lock=PTHREAD_MUTEX_INITIALIZER;pthread_mutex_lock(&lock);}
      if(operation==2){const char byte='x';write(STDOUT_FILENO,&byte,1);}_exit(0);}
    int status=0;CHECK(waitpid(child,&status,0)==child);CHECK(WIFEXITED(status)&&WEXITSTATUS(status)==91+operation);
  }
  CHECK(!BasicSourceQueue<NotLockFree>().Ready());
  auto q=std::unique_ptr<SourceQueue>(new SourceQueue);
  float block[1024]; float out[1024];
  // Filling and wrapping must never overwrite unread or duplicate samples.
  for(uint64_t n=0;n<375;++n) {fill(block,512,n*512);CHECK(q->Push(block,512));}
  CHECK(!q->Push(block,1));
  for(uint64_t n=0;n<800;++n) {
    CHECK(q->Copy(out,512,1.0)==512);
    for(unsigned j=0;j<512;++j) CHECK(out[2*j]==float(n*512+j+1) && out[2*j+1]==-out[2*j]);
    fill(block,512,(n+375)*512);CHECK(q->Push(block,512));
  }
  q.reset();
  auto h=std::unique_ptr<HalCore>(new HalCore);
  CHECK(h->Publish(control())==Status::ok); fill(block,512); CHECK(h->source.Push(block,512));
  auto r=render(*h,input(out)); CHECK(r.consumed_frames==512 && r.copied_frames==512 && r.zero_frames==0);
  for(unsigned n=0;n<1024;++n) CHECK(out[n]==block[n]);
  HalSegment s{}; CHECK(h->evidence.Pop(s));CHECK(s.source_start==0 && s.sequence==1 && s.applied_gain==1);
  r=render(*h,input(out,1)); CHECK(r.reason==Reason::shortfall && r.consumed_frames==0);
  for(float v:out)CHECK(v==0);
  CHECK(h->Publish(control())==Status::retired);
  // Mute advances source at zero gain; pause does not, resume cannot replay.
  h.reset(new HalCore); auto c=control(); c.muted=1;c.supplied_gain=0;
  CHECK(h->Publish(c)==Status::ok);fill(block,512);CHECK(h->source.Push(block,512));
  CHECK(render(*h,input(out)).consumed_frames==512);for(float v:out)CHECK(v==0);
  c.playing=0;c.playback_revision=2;c.playback_epoch=2;CHECK(h->Publish(c)==Status::ok);
  CHECK(render(*h,input(out,1)).consumed_frames==0);
  c.playing=1;c.playback_revision=3;c.playback_epoch=3;c.muted=0;c.supplied_gain=1;c.gain_revision=2;c.gain_epoch=2;
  CHECK(h->Publish(c)==Status::ok);fill(block,512,512);CHECK(h->source.Push(block,512));
  CHECK(render(*h,input(out,2)).consumed_frames==512);CHECK(out[0]==513);
  // Exact Kodi gain curve, and a caller-supplied linear percentage is rejected.
  CHECK(KodiGain(0,false)==0);CHECK(KodiGain(100,false)==1);
  CHECK(std::abs(KodiGain(90,false)-0.5011872336272722)<1e-12);
  CHECK(std::abs(KodiGain(75,false)-0.1778279410038923)<1e-12);
  h.reset(new HalCore); c=control(); c.volume=90;c.supplied_gain=0.9;
  CHECK(h->Publish(c)==Status::invalid);
  // END lease coverage: 1s + ceil(512/48000 seconds) = 1010666667ns.
  for(int delta=-1;delta<=0;++delta) {
    h.reset(new HalCore);c=control();c.request_start_ns=510666667+delta;c.request_end_ns=c.request_start_ns;
    c.issue_ns=c.request_start_ns;c.expiry_ns=1010666667+delta;c.playing=0;CHECK(h->Publish(c)==Status::ok);
    fill(block,512);CHECK(h->source.Push(block,512));r=render(*h,input(out));
    CHECK(r.reason==(delta==0?Reason::none:Reason::lease));CHECK(h->source.Available()==512);
  }
  // Invalid shape/time must zero the known output span without consuming.
  for(int fault=0;fault<9;++fault) {
    h.reset(new HalCore);CHECK(h->Publish(control())==Status::ok);fill(block,512);CHECK(h->source.Push(block,512));
    for(float& v:out)v=0;auto i=input(out); if(fault==0)i.output_bytes=4092; if(fault==1)i.channels=1;
    if(fault==2)i.output_flags=0; if(fault==3)i.output_sample_time=0.5;
    if(fault==4)i.output_sample_time=std::numeric_limits<double>::infinity();
    if(fault==5)i.output_sample_time=9007199254740992.0;
    if(fault==6)i.output_ticks=i.entry_ticks-1;
    if(fault==7)i.output_ticks=i.entry_ticks+100000001;
    if(fault==8)i.timebase_numer=0;
    r=render(*h,i);CHECK(r.consumed_frames==0 && r.reason!=Reason::none);
    CHECK(h->source.Available()==512);for(unsigned n=0;n<i.output_bytes/4;++n)CHECK(out[n]==0);
  }
  // Host continuity permits at most one frame, measured rationally.
  for(int delta: {20833,20834,-20833,-20834}) {
    h.reset(new HalCore);CHECK(h->Publish(control())==Status::ok);fill(block,512);
    CHECK(h->source.Push(block,512));CHECK(h->source.Push(block,512));CHECK(render(*h,input(out)).consumed_frames==512);
    auto i=input(out,1);i.output_ticks=1000000000+10666667+delta;
    r=render(*h,i);CHECK(r.consumed_frames==((delta==20833||delta==-20833)?512u:0u));
  }
  // Wide tick products remain checked; no wrap can make a future timestamp valid.
  uint64_t ns=0;CHECK(!TicksToNs(UINT64_MAX,UINT32_MAX,1,ns));CHECK(!TicksToNs(1,1,0,ns));
  CHECK(TicksToNs(24000000,125,3,ns)&&ns==1000000000);
  // A fractional host nanosecond must not round an expired block lease down.
  h.reset(new HalCore);CHECK(h->Publish(control())==Status::ok);fill(block,2);CHECK(h->source.Push(block,2));CHECK(h->source.Finish());
  auto fraction=input(out);fraction.timebase_numer=1;fraction.timebase_denom=3;
  fraction.output_ticks=3000000002;fraction.entry_ticks=2997000000;fraction.now_ticks=fraction.entry_ticks;fraction.exit_ticks=fraction.entry_ticks;
  CHECK(render(*h,fraction).consumed_frames==2);CHECK(h->FinalEndNs()==1000041668);
  h.reset(new HalCore);c=control();c.request_start_ns=0;c.request_end_ns=0;c.issue_ns=0;c.expiry_ns=500000000;CHECK(h->Publish(c)==Status::ok);
  fill(block,512);CHECK(h->source.Push(block,512));fraction=input(out);fraction.timebase_numer=1;fraction.timebase_denom=3;
  fraction.output_ticks=300000001;fraction.entry_ticks=0;fraction.now_ticks=0;fraction.exit_ticks=0;
  CHECK(render(*h,fraction).reason==Reason::timestamp);
  // Partial EOF advances only source frames; terminal lease expiry is exempt after END.
  h.reset(new HalCore);CHECK(h->Publish(control())==Status::ok);fill(block,13);CHECK(h->source.Push(block,13));CHECK(h->source.Finish());
  r=render(*h,input(out));CHECK(r.consumed_frames==13 && r.copied_frames==13 && r.zero_frames==499);
  for(unsigned n=0;n<26;++n)CHECK(out[n]==block[n]);for(unsigned n=26;n<1024;++n)CHECK(out[n]==0);
  CHECK(h->FinalEndNs()==1000270834);CHECK(h->HoldUntilNs()==2000270834);
  for(unsigned n=1;n<50;++n)CHECK(render(*h,input(out,n)).reason==Reason::none);
  h->Retire(Reason::cancelled);CHECK(h->HoldUntilNs()==0);CHECK(render(*h,input(out,50)).consumed_frames==0);
  // Evidence overflow may not overwrite or advance even one unseen source frame.
  h.reset(new HalCore);c=control();c.playing=0;CHECK(h->Publish(c)==Status::ok);
  for(unsigned n=0;n<4096;++n) {
    c.request_start_ns=900000000+n*32000000ULL/3;c.request_end_ns=c.request_start_ns;c.issue_ns=c.request_start_ns;c.expiry_ns=c.request_start_ns+500000000;c.gain_revision=n+1;
    CHECK(h->Publish(c)==Status::ok);CHECK(render(*h,input(out,n)).reason==Reason::none);
  }
  CHECK(render(*h,input(out,4096)).reason==Reason::evidence_overflow);
  for(unsigned n=0;n<4096;++n){CHECK(h->evidence.Pop(s));CHECK(s.sequence==n+1);}CHECK(!h->evidence.Pop(s));
  // The acquisition point is observable through the real atomic mailbox.
  // Before acquisition: pause/route changes must reject the old generation.
  // After acquisition: the held snapshot stays immutable, permitting one block.
  {
    ControlMailbox box;auto old=control();CHECK(box.Publish(old));EffectiveControl snapshot{};uint64_t generation=0;
    CHECK(box.Read(snapshot,generation));auto paused=old;paused.playing=0;paused.playback_revision=2;paused.playback_epoch=2;
    CHECK(box.Publish(paused));CHECK(!box.Acquire(generation));
    CHECK(box.Read(snapshot,generation));CHECK(box.Acquire(generation));
    CHECK(box.Publish(old));CHECK(snapshot.playing==0 && snapshot.playback_revision==2);
    CHECK(box.Read(snapshot,generation));CHECK(box.Acquire(generation));box.Retire();
    CHECK(snapshot.playing==1);CHECK(!box.Acquire(generation));
  }
  // The injected exit clock runs after copying at the genuine Render boundary.
  // A pause or route change there cannot discard/replay the acquired block.
  for(bool route:{false,true}) {
    h.reset(new HalCore);c=control();CHECK(h->Publish(c)==Status::ok);
    fill(block,512);CHECK(h->source.Push(block,512));fill(block,512,512);CHECK(h->source.Push(block,512));
    boundary_core=h.get();boundary_control=c;boundary_control.playing=0;
    boundary_control.playback_revision=2;boundary_control.playback_epoch=2;boundary_route=route;
    auto i=input(out);i.read_exit_ticks=change_after_acquisition;r=render(*h,i);
    CHECK(r.consumed_frames==512);CHECK(out[0]==1&&out[1022]==512);
    CHECK(h->evidence.Pop(s));CHECK(s.playback_revision==1&&s.playing&&s.control_acquired&&s.exit_ticks==999000001);
    CHECK(render(*h,input(out,1)).consumed_frames==0);CHECK(h->source.Consumed()==512);
    if(!route){c.playback_revision=3;c.playback_epoch=3;CHECK(h->Publish(c)==Status::ok);
      CHECK(render(*h,input(out,2)).consumed_frames==512);CHECK(out[0]==513&&out[1022]==1024);}
  }
  // Actual Render/Publish races must preserve exactly the next unconsumed sample
  // and the record's original applied epoch. Pending snapshots are recorded zeros.
  for(unsigned trial=0;trial<20;++trial) {
    h.reset(new HalCore);c=control();CHECK(h->Publish(c)==Status::ok);
    for(unsigned n=0;n<30;++n){fill(block,512,n*512);CHECK(h->source.Push(block,512));}
    std::atomic<bool> go{false};auto* core=h.get();
    std::thread publisher([&]{while(!go.load()){};auto update=control();for(unsigned n=2;n<2000;++n){update.playback_revision=n;update.playback_epoch=n;update.playing=n%2;CHECK(core->Publish(update)==Status::ok);}});
    go.store(true);uint64_t cursor=0;
    for(unsigned n=0;n<30;++n){auto result=render(*core,input(out,n));CHECK(result.reason==Reason::none);
      CHECK(core->evidence.Pop(s));CHECK(s.sequence==n+1 && s.source_start==cursor);
      if(result.consumed_frames){CHECK(s.playing && s.control_acquired);for(unsigned j=0;j<512;++j)CHECK(out[2*j]==float(cursor+j+1)&&out[2*j+1]==-out[2*j]);cursor+=512;}
      else for(float v:out)CHECK(v==0);
    }
    publisher.join();CHECK(core->source.Consumed()==cursor);
  }
  // Producer and consumer wrap concurrently, exercising real release/acquire handoff.
  q.reset(new SourceQueue);auto* queue=q.get();
  std::thread producer([&]{float data[1024];for(unsigned n=0;n<1200;++n){fill(data,512,n*512);while(!queue->Push(data,512))std::this_thread::yield();}});
  for(unsigned n=0;n<1200;++n){while(queue->Copy(out,512,1)==0)std::this_thread::yield();for(unsigned j=0;j<512;++j)CHECK(out[2*j]==float(n*512+j+1)&&out[2*j+1]==-out[2*j]);}
  producer.join();CHECK(queue->Available()==0);
  std::puts("PASS HAL core: exact FIFO/wrap, fail-closed timing/layout/lease, gain, pause/mute, EOF, overflow");
}
