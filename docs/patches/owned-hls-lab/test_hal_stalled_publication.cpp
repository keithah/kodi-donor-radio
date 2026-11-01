// Deterministic publisher preemption with no production-only hook. Build this
// harness with -fno-builtin-memcpy; the owner pauses at its staging-word copy.
#include "OwnedHalCore.h"
#include <atomic>
#include <thread>
#include <cstdio>
#include <cstdlib>
#include <memory>
#include <chrono>
using namespace owned_hal;
#define CHECK(x) do{if(!(x)){std::fprintf(stderr,"FAIL %d %s\n",__LINE__,#x);std::exit(1);}}while(0)
static thread_local bool publisher=false;
static thread_local unsigned copies=0;
static std::atomic<bool> staged{false},release_stage{false};
extern "C" void* memcpy(void* target,const void* source,size_t size){
  if(publisher&&size==sizeof(EffectiveControl)&&++copies==2){staged.store(true);while(!release_stage.load())std::this_thread::yield();}
  volatile unsigned char* out=static_cast<volatile unsigned char*>(target);const volatile unsigned char* in=static_cast<const volatile unsigned char*>(source);
  for(size_t n=0;n<size;++n)out[n]=in[n];return target;
}
static EffectiveControl control(){EffectiveControl c{};c.device_generation=c.stream_generation=c.playback_revision=c.playback_epoch=c.gain_revision=c.gain_epoch=1;c.request_start_ns=950000000;c.request_end_ns=c.issue_ns=960000000;c.expiry_ns=1450000000;c.volume=100;c.supplied_gain=1;c.started=c.sample_zero_proven=c.playing=1;return c;}
static CallbackInput input(float* out,uint64_t n){CallbackInput i{};i.output=out;i.output_bytes=4096;i.channels=2;i.buffers=1;i.output_sample_time=n*512;i.output_ticks=1000000000+n*32000000/3;i.now_ticks=i.entry_ticks=i.exit_ticks=i.output_ticks-1000000;i.output_flags=i.now_flags=3;i.timebase_numer=i.timebase_denom=1;return i;}
int main(){auto core=std::unique_ptr<HalCore>(new HalCore);float source[5120];for(unsigned n=0;n<5120;++n)source[n]=float(n+1);CHECK(core->source.Push(source,2560));auto old=control();CHECK(core->Publish(old)==Status::ok);float initial[1024]{};CHECK(core->Render(input(initial,0)).consumed_frames==512);HalSegment initial_record;CHECK(core->evidence.Pop(initial_record));auto renewal=old;++renewal.gain_revision;renewal.request_start_ns+=1000000;renewal.request_end_ns+=1000000;renewal.issue_ns+=1000000;renewal.expiry_ns+=1000000;
Status result=Status::invalid;std::thread owner([&]{publisher=true;result=core->Publish(renewal);publisher=false;});const auto deadline=std::chrono::steady_clock::now()+std::chrono::seconds(2);while(!staged.load()&&std::chrono::steady_clock::now()<deadline)std::this_thread::yield();CHECK(staged.load());
float output[1024]{};
for(unsigned n=0;n<3;++n){CHECK(core->Render(input(output,n+1)).consumed_frames==512);CHECK(output[0]==float((n+1)*1024+1));HalSegment row;CHECK(core->evidence.Pop(row));CHECK(row.gain_revision==1&&row.gain_epoch==1);}
// The non-RT publisher remains stalled, yet native pause fences immediately.
core->NativeSpeed(0);CHECK(core->Render(input(output,4)).consumed_frames==0);CHECK(core->source.Consumed()==2048);
release_stage.store(true);owner.join();CHECK(result==Status::ok);CHECK(core->Render(input(output,5)).consumed_frames==0);
core->NativeSpeed(1000);CHECK(core->Render(input(output,6)).consumed_frames==0);
renewal.native_fence=core->NativeFence();++renewal.playback_revision;++renewal.playback_epoch;renewal.resume_playback_revision=renewal.playback_revision;renewal.effective_intent=2;CHECK(core->BeginEffectiveIntent(2)==Status::ok);CHECK(core->Publish(renewal)==Status::ok);CHECK(core->Render(input(output,7)).consumed_frames==512);CHECK(output[0]==4097);
std::puts("PASS stalled unchanged renewal: prior committed snapshot serves three blocks, native pause immediate, resume ordered");}
