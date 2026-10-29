#include "OwnedVideoHost.h"
#include "OwnedPlayerMode.h"
#include <kodi/addon-instance/Inputstream.h>
#include <array>
#include <iostream>
#include <limits>
#include <cstdlib>
#define CHECK(x) do {if(!(x)){std::cerr<<__LINE__<<": "<<#x<<"\n";std::exit(1);}}while(false)
int main() {
 using namespace owned_video;
 kodi::addon::InputstreamInfo source,host;
 source.SetStreamType(INPUTSTREAM_TYPE_AUDIO);source.SetCodecName("aac");source.SetSampleRate(48000);source.SetChannels(2);source.SetExtraData(std::vector<uint8_t>{0x11,0x90});
 CHECK(Project(source,host));CHECK(host.GetStreamType()==INPUTSTREAM_TYPE_VIDEO && host.GetCodecName()=="rawvideo");
 CHECK(host.GetCodecFourCC()==0x30323449 && host.GetWidth()==16 && host.GetHeight()==16 && host.GetAspect()==1);
 CHECK(host.GetFpsRate()==10 && host.GetFpsScale()==1 && host.GetExtraDataSize()==0 && host.GetChannels()==0 && host.GetSampleRate()==0);
 CHECK(source.GetCodecName()=="aac" && source.GetExtraDataSize()==2);source.SetSampleRate(44100);CHECK(!Project(source,host));
 owned_player::State mode;CHECK(mode.Configure("videoplayer_video_v1","VideoPlayer","40"));CHECK(mode.Video() && mode.VideoControl());
 CHECK(mode.Observe(71,0,"VideoPlayer",0,101));int decoded=0;
 CHECK(mode.Arm({true,true,true,true,true,0,0,0,0,0},[&]{++decoded;return true;}));CHECK(decoded==1);
 for(const char* q:{"","39","41","040"}){owned_player::State bad;CHECK(!bad.Configure("videoplayer_video_v1","VideoPlayer",q));}
 Timeline t;CHECK(t.Open(100));CHECK(t.Arm());CHECK(!t.Expired(100+StartupNs-1));
 std::array<uint8_t,386> p;p.fill(0xa5);double pts=0,duration=0;
 for(unsigned i=0;i<60;++i){CHECK(t.Next(101+i,0,false)==Result::Frame);CHECK(t.Fill(p.data()+1,384,101+i,pts,duration));CHECK(pts==i*100000.0 && duration==100000);}
 CHECK(p.front()==0xa5 && p.back()==0xa5);for(unsigned i=1;i<=256;++i)CHECK(p[i]==16);for(unsigned i=257;i<=384;++i)CHECK(p[i]==128);
 CHECK(t.Next(200,0,false)==Result::Retry);CHECK(t.Get().frames==60);
 CHECK(t.Next(201,4799,false)==Result::Retry);CHECK(t.Next(202,4800,false)==Result::Frame);CHECK(t.Fill(p.data()+1,384,202,pts,duration));CHECK(pts==6000000);
 CHECK(t.AcknowledgeStart(203));CHECK(!t.Expired(100+StartupNs));
 CHECK(t.Next(100+StartupNs,4800,false)==Result::Retry); // pause freezes budget, not EOF
 CHECK(t.Drain(100+StartupNs+1));const auto frozen=t.Get().frames;
 CHECK(t.Drain(100+StartupNs+2));CHECK(t.Get().drainNs==100+StartupNs+1 && t.Get().holdDeadline==100+StartupNs+HoldNs+1);
 CHECK(t.Next(100+StartupNs+HoldNs,4800,false)==Result::Retry);
 CHECK(t.Next(100+StartupNs+HoldNs+1,4800,false)==Result::End);CHECK(t.Get().frames==frozen);
 CHECK(t.Next(100+StartupNs+HoldNs+2,4800,false)==Result::End);CHECK(!t.Fill(p.data()+1,384,999,pts,duration));
 Timeline deadline;CHECK(deadline.Open(10));CHECK(deadline.Arm());CHECK(deadline.Expired(10+StartupNs));CHECK(!deadline.AcknowledgeStart(10+StartupNs));
 Timeline missing;CHECK(missing.Open(10));CHECK(missing.Arm());CHECK(missing.Next(10+StartupNs,0,false)==Result::Error);
 Timeline near;CHECK(near.Open(10));CHECK(near.Arm());CHECK(near.AcknowledgeStart(10+StartupNs-1));CHECK(!near.Expired(10+StartupNs));
 Timeline duplicate;CHECK(duplicate.Open(10));CHECK(duplicate.Arm());CHECK(!duplicate.Arm());
 CHECK(!mode.Arm({true,true,true,true,true,0,0,0,0,0},[&]{++decoded;return true;}));CHECK(decoded==1);
 Timeline noarm;CHECK(noarm.Open(10));CHECK(noarm.Next(11,0,false)==Result::Error);
 Timeline overflow;CHECK(!overflow.Open(std::numeric_limits<uint64_t>::max()-StartupNs+1));
 Timeline timer;CHECK(timer.Open(1));CHECK(timer.Arm());CHECK(timer.AcknowledgeStart(2));CHECK(!timer.Drain(std::numeric_limits<uint64_t>::max()-HoldNs+1));
 for(bool drained:{false,true}){Timeline stop;CHECK(stop.Open(10));CHECK(stop.Arm());CHECK(stop.AcknowledgeStart(11));if(drained)CHECK(stop.Drain(12));CHECK(stop.Next(13,0,true)==Result::End);CHECK(stop.Get().frames==0);}
 for(size_t bytes:{size_t(0),size_t(383),size_t(385)}){Timeline bad;CHECK(bad.Open(10));CHECK(bad.Arm());CHECK(bad.Next(11,0,false)==Result::Frame);CHECK(!bad.Fill(p.data()+1,bytes,11,pts,duration));CHECK(bad.Get().frames==0);}
 Timeline bounds;CHECK(bounds.Open(10));CHECK(bounds.Arm());CHECK(bounds.AcknowledgeStart(11));CHECK(bounds.Next(12,std::numeric_limits<uint64_t>::max(),false)==Result::Error);
 Timeline full;CHECK(full.Open(10));CHECK(full.Arm());CHECK(full.AcknowledgeStart(11));
 for(unsigned i=0;i<1860;++i){CHECK(full.Next(12+i,8641536,false)==Result::Frame);CHECK(full.Fill(p.data()+1,384,12+i,pts,duration));}
 CHECK(full.Next(1900,8641536,false)==Result::Retry);CHECK(full.Get().frames==1860);
 std::cout<<"video metadata/payload/budget/startup/drain/cancel/overflow checks passed\n";
}
