#include "OwnedPcmHost.h"
#include "OwnedClockProtocol.h"
#include <kodi/addon-instance/Inputstream.h>
#include <array>
#include <cstdlib>
#include <iostream>
#include <limits>
#include <fstream>
#define CHECK(x) do { if(!(x)) { std::cerr<<__LINE__<<": "<<#x<<"\n";std::exit(1); } } while(false)
int main(int argc,char** argv) {
 kodi::addon::InputstreamInfo source,host;
 source.SetStreamType(INPUTSTREAM_TYPE_AUDIO);source.SetCodecName("aac");source.SetSampleRate(48000);source.SetChannels(2);
 source.SetBitsPerSample(0);source.SetBlockAlign(0);source.SetBitRate(250000);
 source.SetCodecProfile(AACCodecProfileLOW);source.SetCodecFourCC(0x6134706d);source.SetExtraData(std::vector<uint8_t>{0x11,0x90});
 CHECK(owned_pcm::Project(source,host,true));
 CHECK(host.GetCodecName()=="pcm_f32le" && host.GetSampleRate()==48000 && host.GetChannels()==2);
 CHECK(host.GetBitsPerSample()==32 && host.GetBlockAlign()==8 && host.GetBitRate()==3072000);
 CHECK(host.GetExtraDataSize()==0 && host.GetCodecFourCC()==0 && host.GetCodecProfile()==CodecProfileNotNeeded);
 CHECK(source.GetCodecName()=="aac" && source.GetExtraData()==std::vector<uint8_t>({0x11,0x90}) && source.GetBitRate()==250000);
 host=source;CHECK(!owned_pcm::Project(source,host,false));CHECK(host.GetCodecName()=="aac" && host.GetExtraDataSize()==2);
 source.SetSampleRate(44100);CHECK(!owned_pcm::Project(source,host,true));source.SetSampleRate(48000);
 source.SetCodecName("mp3");CHECK(!owned_pcm::Project(source,host,true));source.SetCodecName("aac");
 std::array<uint8_t,30722> payload;payload.fill(0xa5);
 owned_pcm::Timeline timeline;double pts=-1,duration=-1;
 CHECK(timeline.Issue(payload.data()+1,30720,true,false,pts,duration)==owned_pcm::Result::Packet);
 CHECK(pts==0 && duration==80000 && payload.front()==0xa5 && payload.back()==0xa5);
 for(size_t i=1;i+1<payload.size();++i)CHECK(payload[i]==0);
 CHECK(timeline.Issue(payload.data()+1,30720,true,false,pts,duration)==owned_pcm::Result::Packet);CHECK(pts==80000);
 CHECK(timeline.State().probeFrames==7680 && timeline.State().frames==0);
 // Same exact startup contract remains responsible for arming/reset; it cannot
 // be replayed after transfer. PCM probing never creates source-zero evidence.
 owned_clock::StartupSeekProbe seek;owned_clock::StartupSeekState safe{0,false,false,false,true};
 CHECK(seek.Capability(0,0x10,safe));CHECK(seek.Time(1,true,safe));CHECK(seek.Time(0,true,safe));
 timeline.ResetPlayback();
 for(unsigned n=0;n<2251;++n) {
   CHECK(timeline.Issue(payload.data()+1,30720,false,false,pts,duration)==owned_pcm::Result::Packet);
   CHECK(pts==n*80000.0 && duration==80000);
 }
 CHECK(timeline.State().frames==2251ULL*3840 && timeline.State().packets==2251 && timeline.State().probeFrames==7680);
 safe.decodeArmed=true;safe.sourceTransferred=true;CHECK(!seek.Time(0,true,safe));
 // Source EOF alone is not the host terminal condition: final owned audio must
 // drain while the host keeps mixing. Terminal drain/stop/error ends generation.
 CHECK(timeline.Issue(payload.data()+1,30720,false,false,pts,duration)==owned_pcm::Result::Packet);
 const auto frames=timeline.State().frames;
 CHECK(timeline.Issue(payload.data()+1,30720,false,true,pts,duration)==owned_pcm::Result::End);
 CHECK(timeline.Issue(payload.data()+1,30720,false,false,pts,duration)==owned_pcm::Result::End);
 CHECK(timeline.State().frames==frames);
 timeline.ResetPlayback();CHECK(timeline.Issue(payload.data()+1,30720,false,false,pts,duration)==owned_pcm::Result::End);
 for(size_t bad:{size_t{0},size_t{30719},size_t{30721}}) {
   owned_pcm::Timeline invalid;invalid.ResetPlayback();CHECK(invalid.Issue(payload.data()+1,bad,false,false,pts,duration)==owned_pcm::Result::Error);
   CHECK(invalid.State().frames==0 && invalid.State().closed);
 }
 owned_pcm::Timeline null;null.ResetPlayback();CHECK(null.Issue(nullptr,30720,false,false,pts,duration)==owned_pcm::Result::Error);
 owned_pcm::Timeline wrongPhase;CHECK(wrongPhase.Issue(payload.data()+1,30720,false,false,pts,duration)==owned_pcm::Result::Error);
 owned_pcm::Timeline lateProbe;lateProbe.ResetPlayback();CHECK(lateProbe.Issue(payload.data()+1,30720,true,false,pts,duration)==owned_pcm::Result::Error);
 // Cursor rejection is exercised without allocating unbounded packets.
 CHECK(!owned_pcm::ValidCursor(std::numeric_limits<uint64_t>::max()));
 CHECK(!owned_pcm::ValidCursor(owned_pcm::MaxFrames));
 if(argc==2) {std::ofstream out(argv[1]);owned_pcm::WriteJson(out,timeline.State());out<<"\n";CHECK(out.good());}
 std::cout<<"PCM host metadata/payload/timeline/probe/terminal checks passed (NDEBUG-safe)\n";
}
