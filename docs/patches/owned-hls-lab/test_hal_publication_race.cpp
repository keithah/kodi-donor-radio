#include "OwnedHalCore.h"
#include <atomic>
#include <thread>
#include <memory>
#include <cstdio>
#include <cstdlib>
using namespace owned_hal;
#define CHECK(x) do{if(!(x)){std::fprintf(stderr,"FAIL %d %s\n",__LINE__,#x);std::exit(1);}}while(0)
static EffectiveControl command(){EffectiveControl c{};c.device_generation=c.stream_generation=c.playback_revision=c.playback_epoch=c.gain_revision=c.gain_epoch=1;c.request_start_ns=950000000;c.request_end_ns=c.issue_ns=960000000;c.expiry_ns=1450000000;c.volume=100;c.supplied_gain=1;c.started=c.sample_zero_proven=c.playing=1;return c;}
static CallbackInput input(float* out,uint64_t n){CallbackInput i{};i.output=out;i.output_bytes=4096;i.channels=2;i.buffers=1;i.output_sample_time=n*512;i.output_ticks=1000000000+n*32000000/3;i.now_ticks=i.entry_ticks=i.exit_ticks=i.output_ticks-1000000;i.output_flags=i.now_flags=3;i.timebase_numer=i.timebase_denom=1;return i;}
int main(){uint64_t queued=0;
 for(unsigned trial=0;trial<40;++trial){auto core=std::unique_ptr<HalCore>(new HalCore);auto c=command();CHECK(core->Publish(c)==Status::ok);float pcm[1024];for(unsigned n=0;n<30;++n){for(unsigned j=0;j<1024;++j)pcm[j]=float(n*1024+j+1);CHECK(core->source.Push(pcm,512));}
  std::atomic<bool> go{false},stop{false};std::atomic<uint64_t> offers{0};
  std::thread owner([&]{while(!go.load())std::this_thread::yield();auto update=command();uint64_t revision=2;bool holding=false;
    while(!stop.load()){if(!holding){update.gain_revision=update.gain_epoch=update.effective_intent=revision;update.volume=revision%2?75:100;update.supplied_gain=KodiGain(update.volume,false);CHECK(core->BeginEffectiveIntent(revision)==Status::ok);holding=true;}
      const auto result=core->Publish(update);CHECK(result==Status::ok||result==Status::busy);if(result==Status::ok){holding=false;++revision;++offers;}else std::this_thread::yield();}
  });go=true;float out[1024];uint64_t cursor=0;AppliedControlView view;
  for(unsigned n=0;n<30;++n){auto result=core->Render(input(out,n));CHECK(result.consumed_frames==512);HalSegment row;CHECK(core->evidence.Pop(row));CHECK(view.Observe(row));CHECK(row.source_start==cursor&&row.applied.gain_revision==row.gain_revision&&row.applied.supplied_gain==row.applied_gain&&row.applied.expiry_ns==1450000000);
    for(unsigned j=0;j<1024;++j)CHECK(out[j]==static_cast<float>((cursor*2+j+1)*row.applied_gain));cursor+=512;std::this_thread::yield();}
  stop=true;owner.join();queued+=offers.load();CHECK(core->source.Consumed()==15360&&!core->Retired());
 }
 std::printf("PASS concurrent publication:1200 contiguous callbacks, %llu accepted offers, coherent applied tuples and evidence-only owner view\n",static_cast<unsigned long long>(queued));}
