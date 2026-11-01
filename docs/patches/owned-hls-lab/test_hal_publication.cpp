// Production SPSC/Render exercised under deterministic publication/copy stalls.
// -fno-builtin-memcpy keeps the actual staging/consumer copies interposable.
#include "OwnedHalCore.h"
#include <atomic>
#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <memory>
#include <thread>
using namespace owned_hal;
#define CHECK(x) do{if(!(x)){std::fprintf(stderr,"FAIL %d %s\n",__LINE__,#x);std::exit(1);}}while(0)
enum class Hook {none,producer,consumer,offer_after_sample,pause_before_gate,route_before_gate,stop_before_gate};
static thread_local Hook hook=Hook::none;
static thread_local unsigned copy_count=0;
static std::atomic<bool> entered{false},released{false};
static size_t offset=0;
static HalCore* active=nullptr;
static EffectiveControl intervening{};
extern "C" void* memcpy(void* target,const void* source,size_t size){
 const bool control=size==sizeof(EffectiveControl);const unsigned n=control?++copy_count:0;
 const bool park=(hook==Hook::producer&&n==2)||(hook==Hook::consumer&&n==1);
 volatile unsigned char* out=static_cast<volatile unsigned char*>(target);
 const volatile unsigned char* in=static_cast<const volatile unsigned char*>(source);
 for(size_t i=0;i<=size;++i){
  if(park&&i==offset){entered.store(true);while(!released.load())std::this_thread::yield();}
  if(i<size)out[i]=in[i];
 }
 if(control&&n==1&&(hook==Hook::offer_after_sample||hook==Hook::pause_before_gate||hook==Hook::route_before_gate||hook==Hook::stop_before_gate)){
  const auto action=hook;hook=Hook::none;entered.store(true);
  if(action==Hook::offer_after_sample){CHECK(active->Publish(intervening)==Status::ok);}
  else if(action==Hook::pause_before_gate)active->NativeSpeed(0);
  else active->Retire(action==Hook::stop_before_gate?Reason::cancelled:Reason::route);
 }
 return target;
}
static EffectiveControl command(){EffectiveControl c{};c.device_generation=c.stream_generation=c.playback_revision=c.playback_epoch=c.gain_revision=c.gain_epoch=1;c.request_start_ns=950000000;c.request_end_ns=c.issue_ns=960000000;c.expiry_ns=1450000000;c.volume=100;c.supplied_gain=1;c.started=c.sample_zero_proven=c.playing=1;return c;}
static CallbackInput input(float* out,uint64_t n){CallbackInput i{};i.output=out;i.output_bytes=4096;i.channels=2;i.buffers=1;i.output_sample_time=n*512;i.output_ticks=1000000000+n*32000000/3;i.now_ticks=i.entry_ticks=i.exit_ticks=i.output_ticks-1000000;i.output_flags=i.now_flags=3;i.timebase_numer=i.timebase_denom=1;return i;}
static void fill(HalCore& core,uint32_t frames){auto pcm=std::unique_ptr<float[]>(new float[frames*2]);for(uint32_t n=0;n<frames*2;++n)pcm[n]=float(n+1);CHECK(core.source.Push(pcm.get(),frames));}
static void wait_entered(){const auto end=std::chrono::steady_clock::now()+std::chrono::seconds(2);while(!entered.load()&&std::chrono::steady_clock::now()<end)std::this_thread::yield();CHECK(entered.load());}
static void reset(){entered=false;released=false;hook=Hook::none;copy_count=0;}
static HalSegment pop(HalCore& core){HalSegment row;CHECK(core.evidence.Pop(row));return row;}
int main(){float out[1024]{};
 for(bool change:{false,true})for(size_t position:{size_t(0),size_t(1),sizeof(EffectiveControl)/2,sizeof(EffectiveControl)-1,sizeof(EffectiveControl)}){
  reset();auto core=std::unique_ptr<HalCore>(new HalCore);fill(*core,4096);auto old=command();CHECK(core->Publish(old)==Status::ok);CHECK(core->Render(input(out,0)).consumed_frames==512);pop(*core);
  auto next=old;++next.gain_revision;next.request_start_ns+=1000000;next.request_end_ns+=1000000;next.issue_ns+=1000000;next.expiry_ns+=1000000;
  if(change){next.volume=75;next.supplied_gain=KodiGain(75,false);next.gain_epoch=2;next.effective_intent=2;CHECK(core->BeginEffectiveIntent(2)==Status::ok);}
  offset=position;Status result=Status::invalid;
  std::thread owner([&]{hook=Hook::producer;copy_count=0;result=core->Publish(next);hook=Hook::none;});wait_entered();
  for(unsigned n=1;n<=3;++n){CHECK(core->Render(input(out,n)).consumed_frames==512);CHECK(out[0]==float(n*1024+1));auto row=pop(*core);CHECK(row.gain_revision==1&&row.applied.expiry_ns==old.expiry_ns&&row.publication_sequence==1);CHECK(row.applied_intent==1&&row.desired_intent==(change?2u:1u));}
  released=true;owner.join();CHECK(result==Status::ok);CHECK(core->Render(input(out,4)).consumed_frames==512);auto row=pop(*core);CHECK(row.gain_revision==2&&row.applied.expiry_ns==next.expiry_ns&&row.publication_sequence==2);CHECK(out[0]==static_cast<float>(4097*next.supplied_gain));
 }
 // Commit after this callback sampled the empty slot/current snapshot. It must
 // finish current, and only the next boundary may adopt the new publication.
 for(auto action:{Hook::offer_after_sample,Hook::pause_before_gate,Hook::route_before_gate,Hook::stop_before_gate}){
  reset();auto core=std::unique_ptr<HalCore>(new HalCore);fill(*core,2048);auto c=command();CHECK(core->Publish(c)==Status::ok);CHECK(core->Render(input(out,0)).consumed_frames==512);pop(*core);active=core.get();intervening=c;++intervening.gain_revision;
  hook=action;copy_count=0;auto result=core->Render(input(out,1));hook=Hook::none;CHECK(entered.load());
  if(action==Hook::offer_after_sample){CHECK(result.consumed_frames==512&&out[0]==1025);CHECK(pop(*core).gain_revision==1);CHECK(core->Render(input(out,2)).consumed_frames==512);CHECK(pop(*core).gain_revision==2);}
  else {CHECK(result.consumed_frames==0&&core->source.Consumed()==512);if(action==Hook::route_before_gate||action==Hook::stop_before_gate)CHECK(core->Retired());}
 }
 // A pause that wins acquisition with an empty queue is not a shortfall.
 {reset();auto core=std::unique_ptr<HalCore>(new HalCore);fill(*core,512);auto c=command();CHECK(core->Publish(c)==Status::ok);CHECK(core->Render(input(out,0)).consumed_frames==512);pop(*core);active=core.get();hook=Hook::pause_before_gate;copy_count=0;auto r=core->Render(input(out,1));hook=Hook::none;CHECK(entered&&r.reason==Reason::none&&r.consumed_frames==0&&!core->Retired());}
 // Stop after the genuine acquisition/copy permits that one block to finish.
 {auto core=std::unique_ptr<HalCore>(new HalCore);fill(*core,1024);auto c=command();CHECK(core->Publish(c)==Status::ok);active=core.get();auto in=input(out,0);in.read_exit_ticks=[]()noexcept{active->Retire(Reason::cancelled);return uint64_t(1000000000);};CHECK(core->Render(in).consumed_frames==512);CHECK(pop(*core).source_start==0);CHECK(core->Render(input(out,1)).consumed_frames==0&&core->source.Consumed()==512);}
 // An unfinished later expiry cannot extend the applied lease or revive a
 // retired core when the delayed publisher eventually completes.
 {reset();auto core=std::unique_ptr<HalCore>(new HalCore);fill(*core,30000);auto c=command();CHECK(core->Publish(c)==Status::ok);CHECK(core->Render(input(out,0)).consumed_frames==512);auto next=c;++next.gain_revision;next.request_start_ns+=200000000;next.request_end_ns+=200000000;next.issue_ns+=200000000;next.expiry_ns+=200000000;
  offset=sizeof(EffectiveControl)/2;Status result=Status::ok;std::thread owner([&]{hook=Hook::producer;copy_count=0;result=core->Publish(next);});wait_entered();
  unsigned n=1;while(!core->Retired()&&n<60){core->Render(input(out,n++));}CHECK(core->Retired()&&core->Failure()==Reason::lease);const auto consumed=core->source.Consumed();released=true;owner.join();CHECK(result==Status::retired);CHECK(core->Render(input(out,n)).consumed_frames==0&&core->source.Consumed()==consumed);}
 // Hold the consumer halfway through its slot copy. Producer remains Busy;
 // the next slot cannot overwrite any field until the copy's release handoff.
 {reset();ControlMailbox box;auto a=command(),b=a,c=a;b.gain_revision=2;c.gain_revision=3;b.issue_ns+=7;b.volume=75;c.issue_ns+=13;c.volume=90;CHECK(box.TryOffer(a)==Status::ok);offset=sizeof(EffectiveControl)/2;EffectiveControl copied{};uint64_t sequence=0;
  std::thread consumer([&]{hook=Hook::consumer;copy_count=0;CHECK(box.TakePending(copied,sequence));});wait_entered();CHECK(box.TryOffer(b)==Status::busy);released=true;consumer.join();CHECK(sequence==1&&copied.gain_revision==1&&copied.volume==100);CHECK(box.TryOffer(b)==Status::ok);CHECK(box.TryOffer(c)==Status::busy);CHECK(box.TakePending(copied,sequence)&&sequence==2&&copied.issue_ns==b.issue_ns&&copied.volume==75);CHECK(box.TryOffer(c)==Status::ok);CHECK(box.TakePending(copied,sequence)&&sequence==3&&copied.issue_ns==c.issue_ns&&copied.volume==90);}
 // Applied private view can advance only after drain, using that exact record.
 {auto core=std::unique_ptr<HalCore>(new HalCore);fill(*core,2048);AppliedControlView view;auto c=command();CHECK(core->Publish(c)==Status::ok);CHECK(!view.Last());CHECK(core->Render(input(out,0)).consumed_frames==512);CHECK(!view.Last());auto next=c;next.gain_revision=2;next.request_start_ns+=1000000;next.request_end_ns+=1000000;next.issue_ns+=1000000;next.expiry_ns+=1000000;CHECK(core->Publish(next)==Status::ok);CHECK(view.Observe(pop(*core)));CHECK(view.Last()->gain_revision==1&&view.Last()->applied.expiry_ns==c.expiry_ns);CHECK(core->Render(input(out,1)).consumed_frames==512);auto newer=next;newer.gain_revision=3;newer.expiry_ns+=1000000;newer.request_start_ns+=1000000;newer.request_end_ns+=1000000;newer.issue_ns+=1000000;CHECK(core->Publish(newer)==Status::ok);CHECK(view.Last()->gain_revision==1);CHECK(view.Observe(pop(*core)));CHECK(view.Last()->gain_revision==2&&view.Last()->applied.expiry_ns==next.expiry_ns);}
 // An older committed transition retains its identity under a newer pending
 // intent; the owner cannot relabel it or use an undrained later lease.
 {auto core=std::unique_ptr<HalCore>(new HalCore);fill(*core,2048);auto c=command();CHECK(core->Publish(c)==Status::ok);CHECK(core->Render(input(out,0)).consumed_frames==512);pop(*core);
  auto second=c;second.gain_revision=second.gain_epoch=second.effective_intent=2;second.volume=75;second.supplied_gain=KodiGain(75,false);CHECK(core->BeginEffectiveIntent(2)==Status::ok);CHECK(core->Publish(second)==Status::ok);
  auto third=second;third.gain_revision=third.gain_epoch=third.effective_intent=3;third.volume=90;third.supplied_gain=KodiGain(90,false);CHECK(core->BeginEffectiveIntent(3)==Status::ok);CHECK(core->Publish(third)==Status::busy);
  CHECK(core->Render(input(out,1)).consumed_frames==512);auto actual=pop(*core);CHECK(actual.applied_intent==2&&actual.desired_intent==3&&actual.applied.volume==75);CHECK(core->Publish(third)==Status::ok);CHECK(core->Render(input(out,2)).consumed_frames==512);CHECK(pop(*core).applied_intent==3);}
 // Native no-ops and deferred grants preserve existing speed semantics.
 {NativeGate gate;ResumeGrant grant;const auto initial=gate.Load();gate.Speed(1000);gate.Speed(1000);CHECK(gate.Load()==initial);gate.Speed(0);CHECK(grant.Transition(2,110,gate.Load(),100));gate.Speed(0);gate.Speed(1000);CHECK(!grant.Ready(gate.Load(),120));CHECK(!grant.Transition(3,110,gate.Load(),120));CHECK(grant.Transition(4,130,gate.Load(),120));CHECK(grant.Ready(gate.Load(),120));const auto normal=gate.Load();gate.Speed(1000);CHECK(gate.Load()==normal&&grant.Ready(gate.Load(),120));gate.Retire();CHECK(!grant.Ready(gate.Load(),120));}
 // An unchanged gain renewal cannot attach a fresh grant to an old playback.
 {auto core=std::unique_ptr<HalCore>(new HalCore);fill(*core,1024);auto c=command();CHECK(core->Publish(c)==Status::ok);CHECK(core->Render(input(out,0)).consumed_frames==512);core->NativeSpeed(0);core->NativeSpeed(1000);++c.gain_revision;c.native_fence=core->NativeFence();CHECK(core->Publish(c)==Status::invalid);CHECK(core->Render(input(out,1)).consumed_frames==0);}
 std::puts("PASS publication: ten offset stalls, old/new complete tuples, no renewal gaps, boundary races, Busy/copy handoff, lease expiry, evidence-only applied view, strict resume grants");}
