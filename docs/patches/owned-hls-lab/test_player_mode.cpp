#include "OwnedPlayerMode.h"
#include "OwnedClockProtocol.h"
#include "OwnedHostOverride.h"
#include <cstdlib>
#include <iostream>
#define CHECK(x) do {if(!(x)){std::cerr<<__LINE__<<": "<<#x<<"\n";std::exit(1);}}while(false)
int main() {
 using namespace owned_player;
 State s; CHECK(s.Configure("",""));CHECK(!s.Video());CHECK(s.Get().mode=="paplayer_pcm_v1");
 for(const auto& pair : {std::make_pair("other","VideoPlayer"),{"videoplayer_pcm_v1",""},{"","VideoPlayer"},{"videoplayer_pcm_v1","PAPlayer"}}) {
  State bad; CHECK(!bad.Configure(pair.first,pair.second));CHECK(bad.Get().retired);
 }
 auto setup=[](State& v){CHECK(v.Configure("videoplayer_pcm_v1","VideoPlayer"));CHECK(v.Observe(71,0,"VideoPlayer",0,100));};
 owned_override::State noOverride;CHECK(noOverride.Reset(false));CHECK(noOverride.OwnerBegin(99));
 int tokens=0;noOverride.Control(99,2,true,0,10,[&](void*){++tokens;return 0;},11);
 CHECK(!noOverride.Acquire(noOverride.Get().generation,71,"VideoPlayer",true,[&]{++tokens;return owned_override::StartResult{nullptr,0};},12));
 noOverride.Suspend(13);noOverride.ResumeNative();noOverride.Finish(99,[&](void*){++tokens;return 0;},14);
 CHECK(tokens==0 && noOverride.Get().starts==0 && noOverride.Get().ends==0 && noOverride.Get().outstanding==0);
 const std::string expected(64,'a');
 CHECK(VerifiedPacket(492,3,3,0.0,false,expected,expected));
 CHECK(!VerifiedPacket(492,4,3,0.0,false,expected,expected));
 CHECK(!VerifiedPacket(492,3,3,1.0,false,expected,expected));
 CHECK(!VerifiedPacket(492,3,3,0.0,false,expected,std::string(64,'b')));
 CHECK(!VerifiedPacket(492,3,3,0.0,true,expected,expected));
 CHECK(!VerifiedPacket(8193,3,3,0.0,false,expected,expected));
 CHECK(!VerifiedPacket(0,3,3,0.0,false,expected,expected));
 State v;setup(v);CHECK(v.Observe(71,0,"VideoPlayer",0,101));
 CHECK(v.Get().caller==71 && v.Get().observed==100 && !v.Get().armed);
 Proof good{true,true,true,true,true,0,0,0,0,0.0};
 int decoded=0;const int packet=123;const int* seen=nullptr;
 CHECK(v.Arm(good,[&]{seen=&packet;++decoded;return true;}));CHECK(seen==&packet && decoded==1 && v.Get().armed);
 CHECK(!v.Arm(good,[&]{++decoded;return true;}));CHECK(decoded==1 && v.Get().retired);
 for(int n=0;n<10;++n) {
  State bad;setup(bad);auto p=good;
  switch(n){case 0:p.fresh=false;break;case 1:p.normal=false;break;case 2:p.metadata=false;break;case 3:p.raw=false;break;case 4:p.hash=false;break;case 5:p.accepted=1;break;case 6:p.decoded=1;break;case 7:p.queued=1;break;case 8:p.packets=1;break;case 9:p.pts=1;break;}
  int calls=0;CHECK(!bad.Arm(p,[&]{++calls;return true;}));CHECK(calls==0 && bad.Get().retired);
 }
 for(int n=0;n<4;++n){State bad;setup(bad);CHECK(!bad.Observe(n==0?72:71,n==1?-1:0,n==2?"PAPlayer":"VideoPlayer",n==3?-1:0,102));CHECK(bad.Get().retired);}
 State unseen;CHECK(unseen.Configure("videoplayer_pcm_v1","VideoPlayer"));CHECK(!unseen.Arm(good,[]{return true;}));
 State failed;setup(failed);CHECK(!failed.Arm(good,[]{return false;}));CHECK(failed.Get().retired && !failed.Get().armed);
 State stopped;setup(stopped);stopped.Retire();CHECK(!stopped.Observe(71,0,"VideoPlayer",0,103));
 // Actual unchanged authority: resume before a delayed native pause never repairs it.
 owned_clock::Authority a(std::string(32,'a'),std::string(32,'b'));
 auto wire=[](int r,const char* state,int ns){return "v1 "+std::string(32,'a')+" "+std::string(32,'b')+" "+std::to_string(r)+" "+state+" "+std::to_string(ns)+"\n";};
 CHECK(a.Apply(wire(1,"waiting",1),1));CHECK(a.Apply(wire(2,"play",2),2));a.ConfirmSource(true);CHECK(a.Eligible(a.Generation()));
 CHECK(a.Apply(wire(3,"pause",3),3));CHECK(!a.Eligible(a.Generation()));CHECK(a.Apply(wire(4,"resume",4),4));
 a.Speed(0);a.Speed(1000);CHECK(!a.Eligible(a.Generation()));CHECK(a.Apply(wire(4,"resume",4),5));CHECK(!a.Eligible(a.Generation()));
 CHECK(a.Apply(wire(5,"pause",6),6));a.Speed(0);a.Speed(1000);CHECK(a.Apply(wire(6,"resume",7),7));CHECK(a.Eligible(a.Generation()));
 std::cout<<"explicit player mode / retained decode once / identity / pause fences passed\n";
}
