#include "OwnedHalDevice.h"
#include <cstdio>
#include <cstdlib>
#include <atomic>
#include <chrono>
#include <thread>
#include <string>
using namespace owned_hal;
#define CHECK(x) do{if(!(x)){std::fprintf(stderr,"FAIL %d %s\n",__LINE__,#x);std::exit(1);}}while(0)
namespace {
std::string fault;std::atomic<uint64_t> clock_ns{1000000000};std::atomic<unsigned> stop_calls{0},remove_calls{0},destroy_calls{0};
AudioDeviceIOProc callback=nullptr;void* callback_context=nullptr;AudioObjectPropertyListenerProc listener=nullptr;void* listener_context=nullptr;
std::atomic<bool> release_hang{false},tick_entered{false},prepare_entered{false},release_prepare{false};std::atomic<unsigned> read_calls{0};
void prepare_barrier(const char* point)noexcept{if(fault==point){prepare_entered.store(true);while(!release_prepare.load())std::this_thread::yield();}}
Status pin()noexcept{prepare_barrier("cancel_pin");return fault=="pin"?Status::invalid:Status::ok;}
uint64_t now()noexcept{return clock_ns.fetch_add(1000000);}
uint64_t ticks()noexcept{if(fault=="callback_hang"){tick_entered.store(true);while(!release_hang.load())std::this_thread::sleep_for(std::chrono::milliseconds(1));}return 1000000000;}
void wait_tick()noexcept{std::this_thread::sleep_for(std::chrono::microseconds(50));}
OSStatus resolve(DeviceContract& c)noexcept{c.device=75;c.stream=76;c.timebase_numer=1;c.timebase_denom=1;return fault=="resolve"||fault=="cancel_race"?-1:0;}
OSStatus read(const DeviceContract& c)noexcept{CHECK(c.device==75&&c.stream==76);++read_calls;return fault=="read"?-1:0;}
OSStatus add(const DeviceContract&,uint32_t i,AudioObjectPropertyListenerProc p,void* ctx)noexcept{
 listener=p;listener_context=ctx;if(i==3)prepare_barrier("cancel_add");return (fault=="add"&&i==3)||(fault=="delayed_failed"&&i==0)?-1:0;
}
OSStatus remove(const DeviceContract&,uint32_t,AudioObjectPropertyListenerProc,void*)noexcept{++remove_calls;if(fault=="hang_remove")while(!release_hang.load())std::this_thread::sleep_for(std::chrono::milliseconds(1));return fault=="remove"?-1:0;}
OSStatus create(AudioDeviceID,AudioDeviceIOProc p,void* ctx,AudioDeviceIOProcID* id)noexcept{
 CHECK(read_calls>=2);if(fault=="create")return -1;callback=p;callback_context=ctx;*id=reinterpret_cast<AudioDeviceIOProcID>(uintptr_t(1));prepare_barrier("cancel_create");return 0;
}
OSStatus start(AudioDeviceID,AudioDeviceIOProcID)noexcept{return fault=="start"?-1:0;}
OSStatus stop(AudioDeviceID,AudioDeviceIOProcID)noexcept{
 ++stop_calls;if(fault=="hang")while(!release_hang.load())std::this_thread::sleep_for(std::chrono::milliseconds(1));
 return fault=="stop"?-1:0;
}
OSStatus destroy(AudioDeviceID,AudioDeviceIOProcID)noexcept{++destroy_calls;if(fault=="hang_destroy")while(!release_hang.load())std::this_thread::sleep_for(std::chrono::milliseconds(1));return fault=="destroy"?-1:0;}
DeviceApi api(){DeviceApi a{};a.pin=pin;a.now_ns=now;a.ticks=ticks;a.wait_tick=wait_tick;a.resolve=resolve;a.read=read;a.add=add;a.remove=remove;a.create=create;a.start=start;a.stop=stop;a.destroy=destroy;return a;}
}
int main(int argc,char** argv){
 CHECK(argc==2);fault=argv[1];HalDevice device(api());
 if(fault=="cancel_race") {
  for(unsigned trial=0;trial<50;++trial){
   HalSessionState state;HalDevice racing(api());std::atomic<bool> go{false};Status prepared=Status::ok;
   std::thread preparation([&]{while(!go.load())std::this_thread::yield();prepared=racing.Prepare(state);});
   std::thread cancellation([&]{while(!go.load())std::this_thread::yield();racing.RequestStop();});
   go.store(true);preparation.join();cancellation.join();
   CHECK(prepared!=Status::ok);CHECK(state.Core().Retired());CHECK(racing.Start()!=Status::ok);CHECK(state.Core().source.Consumed()==0);
   CHECK(racing.Cleanup(UINT64_MAX)==CleanupResult::clean);CHECK(!Quarantined());
   bool free=false;for(unsigned spin=0;spin<100000&&!free;++spin){free=ModuleGate().Claim();if(!free)std::this_thread::yield();}
   CHECK(free);ModuleGate().Release();
  }
  CHECK(remove_calls==0&&destroy_calls==0&&stop_calls==0);
  std::puts("PASS cancellation/publication race: 50 concurrent starts, no source advance or exposure");return 0;
 }
 if(fault=="never_exposed_second") {
  fault="resolve";HalSessionState first;HalDevice old(api());CHECK(old.Prepare(first)==Status::invalid);
  CHECK(old.Cleanup(UINT64_MAX)==CleanupResult::clean);CHECK(!Quarantined());
  fault="ok";HalSessionState second;HalDevice next(api());Status prepared=Status::invalid;
  for(unsigned n=0;n<1000&&prepared==Status::invalid;++n){prepared=next.Prepare(second);if(prepared==Status::invalid)std::this_thread::yield();}
  CHECK(prepared==Status::ok);CHECK(next.Start()==Status::ok);
  CHECK(old.Cleanup(0)==CleanupResult::clean);CHECK(!Quarantined());
  CHECK(next.Cleanup(UINT64_MAX)==CleanupResult::clean);CHECK(Quarantined());
  std::puts("PASS never-exposed clean admits second session; old expired cleanup cannot quarantine it");return 0;
 }
 if(fault=="delayed_entry"||fault=="delayed_failed") {
  auto retained_state=std::unique_ptr<HalSessionState>(new HalSessionState);
  auto owner=std::unique_ptr<HalDevice>(new HalDevice(api()));HalCore* core=&retained_state->Core();
  CHECK(owner->Prepare(*retained_state)==(fault=="delayed_entry"?Status::ok:Status::invalid));
  if(fault=="delayed_entry")CHECK(owner->Start()==Status::ok);
  float source[1024];for(float& sample:source)sample=123;CHECK(core->source.Push(source,512));
  CHECK(owner->Cleanup(UINT64_MAX)==CleanupResult::clean);CHECK(Quarantined());
  retained_state.reset();owner.reset(); // only retained registration context remains
  AudioObjectPropertyAddress changed{};CHECK(listener(75,1,&changed,listener_context)==0);
  float samples[1024]{};AudioBufferList buffers{};buffers.mNumberBuffers=1;buffers.mBuffers[0].mNumberChannels=2;buffers.mBuffers[0].mDataByteSize=4096;buffers.mBuffers[0].mData=samples;
  AudioTimeStamp stamp{};stamp.mFlags=3;stamp.mHostTime=1000000000;
  if(callback)CHECK(callback(75,&stamp,&buffers,&stamp,&buffers,&stamp,callback_context)==0);
  for(float sample:samples)CHECK(sample==0);CHECK(core->source.Consumed()==0);
  HalSessionState second;HalDevice contender(api());CHECK(contender.Prepare(second)==Status::quarantined);
  CHECK(remove_calls==(fault=="delayed_entry"?kGuardCount:0u)&&destroy_calls==(fault=="delayed_entry"?1u:0u)&&stop_calls==(fault=="delayed_entry"?1u:0u));
  std::puts("PASS delayed entry after successful cleanup and owner destruction: retained revoked context, second session denied");return 0;
 }
 if(fault=="cancel_before"||fault=="cancel_pin"||fault=="cancel_add"||fault=="cancel_create") {
  HalSessionState state;Status prepared=Status::invalid;
  if(fault=="cancel_before"){device.RequestStop();prepared=device.Prepare(state);}
  else {std::thread preparing([&]{prepared=device.Prepare(state);});while(!prepare_entered.load())std::this_thread::yield();device.RequestStop();release_prepare.store(true);preparing.join();}
  CHECK(prepared==Status::retired);CHECK(device.Start()!=Status::ok);CHECK(state.Core().Retired());
  float pcm[1024];for(float& v:pcm)v=123;CHECK(state.Core().source.Push(pcm,512));
  float output[1024]{};CallbackInput in{};in.output=output;in.output_bytes=4096;in.channels=2;in.buffers=1;
  CHECK(state.Core().Render(in).consumed_frames==0);for(float v:output)CHECK(v==0);CHECK(state.Core().source.Consumed()==0);
  CHECK(device.Cleanup(UINT64_MAX)==CleanupResult::clean);CHECK(stop_calls==0);CHECK(Quarantined()==(fault=="cancel_add"||fault=="cancel_create"));
  CHECK(remove_calls==(fault=="cancel_add"?4u:(fault=="cancel_create"?kGuardCount:0u)));
  CHECK(destroy_calls==(fault=="cancel_create"?1u:0u));
  std::printf("PASS device %s: irreversible cancellation across prepare publication/registration\n",argv[1]);return 0;
 }
 {
  HalSessionState state;Status prepared=device.Prepare(state);
  if(fault=="pin"||fault=="resolve"||fault=="read"||fault=="add"||fault=="create") {
    CHECK(prepared==Status::invalid);CHECK(device.Start()!=Status::ok);
    const uint64_t before_cleanup=clock_ns.load();
    CHECK(device.Cleanup(UINT64_MAX)==CleanupResult::clean);CHECK(Quarantined()==(fault=="read"||fault=="add"||fault=="create"));
    if(fault=="pin")CHECK(clock_ns.load()==before_cleanup); // no worker may outlive a failed self-pin

    CHECK(remove_calls==(fault=="add"?3u:(fault=="read"||fault=="create"?kGuardCount:0u)));CHECK(destroy_calls==0);
    std::puts("PASS prepare failure: no active callbacks or leaked registration");return 0;
  }
  CHECK(prepared==Status::ok);CHECK(callback&&listener);
  CHECK(device.Start()==(fault=="start"?Status::invalid:Status::ok));
  if(fault=="route") {
   AudioObjectPropertyAddress changed{};CHECK(listener(75,1,&changed,listener_context)==0);CHECK(state.Core().Retired());CHECK(state.Core().Failure()==Reason::route);
  }
  // Real callback entry point must be independent of the facade's lifetime.
 }
 float samples[1026]{};samples[1024]=123;samples[1025]=456;
 AudioBufferList buffers{};buffers.mNumberBuffers=1;buffers.mBuffers[0].mNumberChannels=2;buffers.mBuffers[0].mDataByteSize=4096;buffers.mBuffers[0].mData=samples;
 AudioTimeStamp stamp{};stamp.mFlags=kAudioTimeStampSampleTimeValid|kAudioTimeStampHostTimeValid;stamp.mHostTime=1000000000;
 if(fault=="layout")buffers.mBuffers[0].mDataByteSize=UINT32_MAX;
 if(fault=="buffers")buffers.mNumberBuffers=UINT32_MAX;
 std::thread active;
 if(fault=="callback_hang") {active=std::thread([&]{CHECK(callback(75,&stamp,&buffers,&stamp,&buffers,&stamp,callback_context)==0);});while(!tick_entered.load())std::this_thread::yield();}
 else CHECK(callback(75,&stamp,&buffers,&stamp,&buffers,&stamp,callback_context)==0);
 for(unsigned n=0;n<1024;++n)CHECK(samples[n]==0);CHECK(samples[1024]==123&&samples[1025]==456);
 device.RequestStop();auto result=device.Cleanup(now()+2000000000);
 if(fault=="stop"||fault=="remove"||fault=="destroy") {
  CHECK(result==CleanupResult::failed);CHECK(Quarantined());HalSessionState next;HalDevice blocked(api());CHECK(blocked.Prepare(next)==Status::quarantined);
 }else if(fault=="hang"||fault=="hang_remove"||fault=="hang_destroy"||fault=="callback_hang") {
  CHECK(result==CleanupResult::timeout_quarantined);CHECK(Quarantined());CHECK(stop_calls==1);
  // The facade is gone; the retained OS callback still accesses valid state.
  if(fault!="callback_hang")CHECK(callback(75,&stamp,&buffers,&stamp,&buffers,&stamp,callback_context)==0);
  release_hang.store(true);if(active.joinable())active.join();for(unsigned n=0;n<100&&destroy_calls==0;++n)std::this_thread::sleep_for(std::chrono::milliseconds(1));CHECK(destroy_calls==1);
  for(unsigned n=0;n<100&&remove_calls<kGuardCount;++n)std::this_thread::sleep_for(std::chrono::milliseconds(1));
  CHECK(device.Cleanup(UINT64_MAX)==CleanupResult::timeout_quarantined);
  HalSessionState next;HalDevice blocked(api());CHECK(blocked.Prepare(next)==Status::quarantined);
 }else {CHECK(result==CleanupResult::clean);CHECK(Quarantined());CHECK(destroy_calls==1);CHECK(remove_calls==kGuardCount);}
 std::printf("PASS device %s: actual wrapper, retained independent context, bounded cleanup\n",argv[1]);
}
