// Compile the real device wrapper into this unloadable test image. Its API table
// below replaces only OS calls; no real CoreAudio registration/start occurs.
#include "OwnedHalDevice.cpp"
#include <atomic>
#include <memory>
namespace {
using namespace owned_hal;
std::atomic<int> calls{0};std::atomic<uint64_t> clock_ns{1000000000};
std::atomic<bool> release_cleanup{false};
AudioDeviceIOProc retained=nullptr;void* retained_context=nullptr;
std::unique_ptr<HalSessionState> session;std::unique_ptr<HalDevice> owner;
uint64_t FakeNow()noexcept{return clock_ns.fetch_add(100000000);}
uint64_t FakeTicks()noexcept{return 1000000000;}
void FakeWait()noexcept{std::this_thread::sleep_for(std::chrono::milliseconds(1));}
OSStatus FakeResolve(DeviceContract& c)noexcept{c.device=75;c.stream=76;c.timebase_numer=1;c.timebase_denom=1;return 0;}
OSStatus FakeRead(const DeviceContract&)noexcept{return 0;}
OSStatus FakeListener(const DeviceContract&,uint32_t,AudioObjectPropertyListenerProc,void*)noexcept{return 0;}
OSStatus FakeCreate(AudioDeviceID,AudioDeviceIOProc cb,void* ctx,AudioDeviceIOProcID* id)noexcept{retained=cb;retained_context=ctx;*id=reinterpret_cast<AudioDeviceIOProcID>(uintptr_t(1));return 0;}
OSStatus FakeStart(AudioDeviceID,AudioDeviceIOProcID)noexcept{return 0;}
OSStatus FakeStop(AudioDeviceID,AudioDeviceIOProcID)noexcept{while(!release_cleanup.load())FakeWait();return 0;}
OSStatus FakeDestroy(AudioDeviceID,AudioDeviceIOProcID)noexcept{return 0;}
}
extern "C" __attribute__((visibility("default"))) int retained_callback() {
 if(retained){float samples[1024]{};AudioBufferList buffers{};buffers.mNumberBuffers=1;buffers.mBuffers[0].mNumberChannels=2;buffers.mBuffers[0].mDataByteSize=4096;buffers.mBuffers[0].mData=samples;
 AudioTimeStamp stamp{};stamp.mFlags=3;stamp.mHostTime=1000000000;
 if(retained(75,&stamp,&buffers,&stamp,&buffers,&stamp,retained_context)!=0)return -1;
 for(float sample:samples)if(sample!=0)return -2;}
 return ++calls;
}
extern "C" __attribute__((visibility("default"))) int pin_module() {return owned_hal::PinOwnModule()==owned_hal::Status::ok?1:0;}
extern "C" __attribute__((visibility("default"))) unsigned pin_count() {return owned_hal::OwnModulePinCount();}
extern "C" __attribute__((visibility("default"))) int start_fake() {
 DeviceApi api{};api.pin=PinOwnModule;api.now_ns=FakeNow;api.ticks=FakeTicks;api.wait_tick=FakeWait;api.resolve=FakeResolve;api.read=FakeRead;
 api.add=FakeListener;api.remove=FakeListener;api.create=FakeCreate;api.start=FakeStart;api.stop=FakeStop;api.destroy=FakeDestroy;
 session.reset(new HalSessionState);owner.reset(new HalDevice(api));return owner->Prepare(*session)==Status::ok&&owner->Start()==Status::ok;
}
extern "C" __attribute__((visibility("default"))) int drop_owner() {
 session.reset();owner.reset();return Quarantined()?1:0;
}
extern "C" __attribute__((visibility("default"))) void release_fake_cleanup() {release_cleanup.store(true);}
