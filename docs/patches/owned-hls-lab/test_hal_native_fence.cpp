#include "OwnedHalCore.h"
#include <cstdio>
#include <cstdlib>
#include <memory>
using namespace owned_hal;
#define CHECK(x) do{if(!(x)){std::fprintf(stderr,"FAIL %d %s\n",__LINE__,#x);std::exit(1);}}while(0)
static EffectiveControl command(){EffectiveControl c{};c.device_generation=c.stream_generation=c.playback_revision=c.playback_epoch=c.gain_revision=c.gain_epoch=1;c.request_start_ns=950000000;c.request_end_ns=c.issue_ns=960000000;c.expiry_ns=1450000000;c.volume=100;c.supplied_gain=1;c.started=c.sample_zero_proven=c.playing=1;return c;}
static CallbackInput input(float* out,uint64_t n){CallbackInput i{};i.output=out;i.output_bytes=4096;i.channels=2;i.buffers=1;i.output_sample_time=n*512;i.output_ticks=1000000000+n*32000000/3;i.now_ticks=i.entry_ticks=i.exit_ticks=i.output_ticks-1000000;i.output_flags=i.now_flags=3;i.timebase_numer=i.timebase_denom=1;return i;}
static HalCore* active=nullptr;
static uint64_t pause_after()noexcept{active->NativeSpeed(0);return 1000000000;}
int main(){auto core=std::unique_ptr<HalCore>(new HalCore);float pcm[4096];for(unsigned n=0;n<4096;++n)pcm[n]=float(n+1);float out[1024]{};
CHECK(core->source.Push(pcm,2048));auto c=command();CHECK(core->Publish(c)==Status::ok);
core->NativeSpeed(0);CHECK(core->Render(input(out,0)).consumed_frames==0);CHECK(!core->Retired());
// No owner service: native resume alone cannot revive the prior playing snapshot.
core->NativeSpeed(1000);CHECK(core->Render(input(out,1)).consumed_frames==0);
// An unchanged gain renewal cannot acknowledge a native resume generation.
++c.gain_revision;CHECK(core->Publish(c)==Status::ok);CHECK(core->Render(input(out,2)).consumed_frames==0);
// Only an explicit later playback resume acknowledges the observed native token.
c.native_fence=core->NativeFence();++c.playback_revision;++c.playback_epoch;c.resume_playback_revision=c.playback_revision;c.effective_intent=c.playback_epoch;CHECK(core->BeginEffectiveIntent(c.effective_intent)==Status::ok);CHECK(core->Publish(c)==Status::ok);
CHECK(core->Render(input(out,3)).consumed_frames==512);CHECK(out[0]==1);
// Pause after acquisition may finish exactly its already-acquired block.
active=core.get();auto i=input(out,4);i.read_exit_ticks=pause_after;CHECK(core->Render(i).consumed_frames==512);CHECK(out[0]==1025);
CHECK(core->Render(input(out,5)).consumed_frames==0);CHECK(core->source.Consumed()==1024);
core->NativeSpeed(1000);const auto stale=core->NativeFence();core->NativeSpeed(0);core->NativeSpeed(1000);
c.native_fence=stale;++c.playback_revision;++c.playback_epoch;c.resume_playback_revision=c.playback_revision;c.effective_intent=c.playback_epoch;CHECK(core->BeginEffectiveIntent(c.effective_intent)==Status::ok);CHECK(core->Publish(c)==Status::ok);CHECK(core->Render(input(out,6)).consumed_frames==0);
c.native_fence=core->NativeFence();++c.playback_revision;++c.playback_epoch;c.resume_playback_revision=c.playback_revision;c.effective_intent=c.playback_epoch;CHECK(core->BeginEffectiveIntent(c.effective_intent)==Status::ok);CHECK(core->Publish(c)==Status::ok);CHECK(core->Render(input(out,7)).consumed_frames==512);CHECK(out[0]==2049);
core->NativeSpeed(1500);CHECK(core->Retired());CHECK(core->Render(input(out,8)).consumed_frames==0);
// Resume notification may precede native speed1000; its later coherent
// publication must acknowledge once without requiring a fabricated callback.
auto reverse=std::unique_ptr<HalCore>(new HalCore);CHECK(reverse->source.Push(pcm,2048));auto rc=command();CHECK(reverse->Publish(rc)==Status::ok);CHECK(reverse->Render(input(out,0)).consumed_frames==512);
reverse->NativeSpeed(0);rc.playing=0;++rc.playback_revision;++rc.playback_epoch;rc.effective_intent=2;CHECK(reverse->BeginEffectiveIntent(2)==Status::ok);CHECK(reverse->Publish(rc)==Status::ok);CHECK(reverse->Render(input(out,1)).consumed_frames==0);
reverse->NativeSpeed(1000);rc.playing=1;rc.native_fence=reverse->NativeFence();rc.resume_playback_revision=rc.playback_revision;CHECK(reverse->Publish(rc)==Status::ok);CHECK(reverse->Render(input(out,2)).consumed_frames==512);
ResumeGrant grant;NativeGate gate;gate.Speed(0);CHECK(grant.Transition(2,100,gate.Load(),90));gate.Speed(1000);CHECK(grant.Ready(gate.Load(),90));gate.Speed(0);gate.Speed(1000);CHECK(!grant.Ready(gate.Load(),110));CHECK(!grant.Transition(2,100,gate.Load(),110));

std::puts("PASS native-speed coherent token, stalled owner, stale resume, pre/post-acquisition source order");}
