// Portable lifecycle checks; no scheduling APIs and CHECK is active under NDEBUG.
#include "OwnedHostOverride.h"
#include "OwnedStallDiagnostics.h"
#include "OwnedClockProtocol.h"
#include <fstream>
#include <sstream>
#include <cstdlib>
#include <iostream>
#include <thread>
#include <mutex>
#include <condition_variable>
#define CHECK(x) do { if(!(x)) { std::cerr<<__LINE__<<": "<<#x<<"\n";std::exit(1); } } while(false)
using namespace owned_override;
struct Fake {
 int startCalls=0,endCalls=0,startStatus=0,endStatus=0;
 int token=1;
 StartResult Start() { ++startCalls;return {startStatus ? nullptr : &token,startStatus}; }
 int End(void* ptr) { CHECK(ptr==&token);++endCalls;return endStatus; }
};
void Active(State& s,Fake& os) {
 CHECK(s.Reset(true));CHECK(s.OwnerBegin(7));
 s.Control(7,1,false,s.Get().suspensionEpoch,100,[&](void* p){return os.End(p);},10);
 s.Control(7,2,true,s.Get().suspensionEpoch,100,[&](void* p){return os.End(p);},20);
}
bool Call(State& s,Fake& os,uint64_t id=42,const char* name="PAPlayer",bool ready=true) {
 return s.Acquire(s.Get().generation,id,name,ready,[&]{return os.Start();},30);
}
int main(int argc,char** argv) {
 Snapshot capturedActive,capturedClosed;
 // The actual authority and override publisher are separate operations. Enumerate
 // pause before Apply, after Apply, and a queued command first read after pause.
 for(bool existingToken:{false,true}) for(int interleaving:{0,1,2}) {
   State s;Fake os;Active(s,os);
   const std::string nonce(32,'a'),incarnation(32,'b');
   owned_clock::Authority authority(nonce,incarnation);
   auto apply=[&](unsigned revision,const char* state,uint64_t issued,uint64_t now) {
     CHECK(authority.Apply("v1 "+nonce+" "+incarnation+" "+std::to_string(revision)+" "+state+" "+std::to_string(issued)+"\n",now));
   };
   auto publish=[&](uint64_t epoch,uint64_t now) {
     const auto& command=authority.Control();
     s.Control(7,command.revision,command.state=="play" || command.state=="resume",
         epoch,command.issued,[&](void* p){return os.End(p);},now);
   };
   auto pause=[&] { authority.Speed(0);s.Suspend(50); };
   apply(1,"waiting",10,11);apply(2,"play",20,21);
   if(existingToken)CHECK(Call(s,os));
   else { apply(3,"pause",30,31);publish(s.Get().suspensionEpoch,32); }
   const unsigned revision=existingToken ? 3 : 4;
   if(interleaving==2)pause(); // queued resume predates pause but read afterward
   const auto epoch=s.Get().suspensionEpoch; // before Authority::Apply
   if(interleaving==0)pause();
   apply(revision,"resume",40,interleaving==1 ? 45 : 55);
   if(interleaving==1)pause();
   publish(epoch,60);
   CHECK(!s.Get().allowed && s.Get().outstanding==0);
   CHECK(os.endCalls==(existingToken ? 1 : 0));
   CHECK(!Call(s,os));
   authority.Speed(1000);s.ResumeNative();
   publish(s.Get().suspensionEpoch,61); // polling the same applied revision
   CHECK(!Call(s,os));CHECK(s.Get().blockedThrough==revision);
   CHECK(authority.Apply("v1 "+nonce+" "+incarnation+" "+std::to_string(revision)+" resume 40\n",62));
   publish(s.Get().suspensionEpoch,63);CHECK(!Call(s,os)); // identical replay
   apply(revision+1,"resume",70,71);publish(s.Get().suspensionEpoch,72);
   CHECK(Call(s,os)); // only a genuinely post-pause validated command restores it
   s.Finish(7,[&](void* p){return os.End(p);},90);
   CHECK(os.startCalls==os.endCalls && s.Get().outstanding==0);
 }
 // Fast native pause/resume cannot erase the owner's obligation to end the
 // old token, even when fresh validated resume publication beats owner service.
 for(bool existingToken:{false,true}) for(unsigned rapidPauses:{1U,3U}) {
   State s;Fake os;Active(s,os);
   const std::string nonce(32,'a'),incarnation(32,'b');
   owned_clock::Authority authority(nonce,incarnation);
   auto apply=[&](unsigned revision,const char* state,uint64_t issued) {
     CHECK(authority.Apply("v1 "+nonce+" "+incarnation+" "+std::to_string(revision)+" "+state+" "+std::to_string(issued)+"\n",issued+1));
   };
   apply(1,"waiting",10);apply(2,"play",20);
   if(existingToken)CHECK(Call(s,os));
   for(unsigned cycle=0;cycle<2;++cycle) {
     const int oldStarts=os.startCalls,oldEnds=os.endCalls;
     const bool hadToken=s.Get().outstanding!=0;
     for(unsigned pause=0;pause<rapidPauses;++pause) {
       authority.Speed(0);s.Suspend(50+cycle*100+pause*2);
       authority.Speed(1000);s.ResumeNative();
       CHECK(!Call(s,os));CHECK(os.endCalls==oldEnds);
     }
     const auto epoch=s.Get().suspensionEpoch;
     apply(3+cycle,"resume",60+cycle*100);
     const auto& command=authority.Control();
     auto publish=[&] { s.Control(7,command.revision,true,epoch,command.issued,
         [&](void* p){return os.End(p);},70+cycle*100); };
     publish();
     CHECK(os.endCalls==oldEnds+(hadToken ? 1 : 0));
     CHECK(s.Get().outstanding==0 && !s.Get().active && s.Get().allowed);
     CHECK(os.startCalls==oldStarts); // cleanup precedes any fresh acquisition
     publish();CHECK(os.endCalls==oldEnds+(hadToken ? 1 : 0));
     CHECK(Call(s,os));CHECK(os.startCalls==oldStarts+1);
   }
   s.Finish(7,[&](void* p){return os.End(p);},300);
   CHECK(os.startCalls==os.endCalls && s.Get().outstanding==0);
 }
 // Deferred pause cleanup consumes an erroring end exactly once and fails closed.
 { State s;Fake os;Active(s,os);CHECK(Call(s,os));
   s.Suspend(50);s.ResumeNative();os.endStatus=22;
   s.Control(7,3,true,s.Get().suspensionEpoch,60,[&](void* p){return os.End(p);},70);
   CHECK(os.endCalls==1 && s.Get().failed && s.Get().outstanding==0);
   CHECK(!Call(s,os));
   s.Control(7,4,true,s.Get().suspensionEpoch,80,[&](void* p){return os.End(p);},90);
   s.Finish(7,[&](void* p){return os.End(p);},100);
   CHECK(os.endCalls==1 && s.Get().endAttempts==1 && s.Get().ends==0);
 }
 // Each repeated native pause invalidates a transaction, even while already paused.
 { State s;Fake os;Active(s,os);s.Suspend(50);auto epoch=s.Get().suspensionEpoch;
   s.Suspend(60);CHECK(s.Get().suspensionEpoch>epoch);
   s.Control(7,3,true,epoch,70,[&](void* p){return os.End(p);},80);
   s.ResumeNative();s.Control(7,3,true,s.Get().suspensionEpoch,70,[&](void* p){return os.End(p);},81);
   CHECK(!Call(s,os));CHECK(s.Get().blockedThrough==3);
   s.Finish(7,[&](void* p){return os.End(p);},90);
 }
 // A fresh post-pause control may arrive before native resume, but cannot boost
 // a paused target. Native resume alone cannot undo a stale revision barrier.
 { State s;Fake os;Active(s,os);CHECK(Call(s,os));s.Suspend(50);
   s.Control(7,3,true,s.Get().suspensionEpoch,60,[&](void* p){return os.End(p);},70);
   CHECK(s.Get().allowed && !s.Get().active && s.Get().outstanding==0);
   CHECK(!Call(s,os));s.ResumeNative();CHECK(Call(s,os));
   s.Finish(7,[&](void* p){return os.End(p);},90);CHECK(os.startCalls==os.endCalls);
 }
 { State s;Fake os;Active(s,os);
   CHECK(!Call(s,os,9,"JobWorker",false));CHECK(!Call(s,os,9,"JobWorker"));
   CHECK(os.startCalls==0 && !s.Get().failed);
   CHECK(Call(s,os));CHECK(Call(s,os));
   CHECK(os.startCalls==1 && s.Get().starts==1 && s.Get().outstanding==1);
   capturedActive=s.Get();
   CHECK(s.Get().targetId==42 && s.Get().maxOutstanding==1 && s.Get().active);
   CHECK(!s.Reset(true)); // outstanding token cannot be forgotten
   s.Control(7,3,false,s.Get().suspensionEpoch,100,[&](void* p){return os.End(p);},40);
   CHECK(os.endCalls==1 && s.Get().outstanding==0 && !s.Get().active);
   s.Control(7,3,false,s.Get().suspensionEpoch,100,[&](void* p){return os.End(p);},41);CHECK(os.endCalls==1);
   CHECK(!Call(s,os));
   s.Control(7,4,true,s.Get().suspensionEpoch,100,[&](void* p){return os.End(p);},50);CHECK(Call(s,os));
   s.Finish(7,[&](void* p){return os.End(p);},60);
   CHECK(s.Get().starts==2 && s.Get().ends==2 && s.Get().outstanding==0);
   capturedClosed=s.Get();
   CHECK(!Call(s,os));CHECK(s.Reset(true));CHECK(s.Get().targetId==0 && s.Get().starts==0);
 }
 { State s;Fake os;Active(s,os);auto stale=s.Get().generation;
   s.Suspend(50);s.Control(7,2,true,s.Get().suspensionEpoch,100,[&](void* p){return os.End(p);},40);
   CHECK(!s.Acquire(stale,42,"PAPlayer",true,[&]{return os.Start();},50));
   CHECK(!Call(s,os));CHECK(os.startCalls==0);
   s.Control(7,3,false,s.Get().suspensionEpoch,100,[&](void* p){return os.End(p);},60);
   s.ResumeNative();s.Control(7,4,true,s.Get().suspensionEpoch,100,[&](void* p){return os.End(p);},70);CHECK(Call(s,os));
   s.Finish(7,[&](void* p){return os.End(p);},80);
 }
 { State s;Fake os;Active(s,os);CHECK(Call(s,os));
   CHECK(!Call(s,os,43));CHECK(s.Get().failed && !s.Get().active && os.endCalls==0);
   s.Control(7,2,true,s.Get().suspensionEpoch,100,[&](void* p){return os.End(p);},40);
   CHECK(os.endCalls==1 && s.Get().outstanding==0);CHECK(!Call(s,os));
   s.Finish(7,[&](void* p){return os.End(p);},50);
 }
 for(const char* name:{"Other","PAPlayerX",""}) { State s;Fake os;Active(s,os);
   CHECK(!Call(s,os,42,name));CHECK(s.Get().failed && os.startCalls==0);
   s.Finish(7,[&](void* p){return os.End(p);},40); }
 for(uint64_t id:{uint64_t{0},uint64_t{7}}) { State s;Fake os;Active(s,os);
   CHECK(!Call(s,os,id));CHECK(s.Get().failed && os.startCalls==0);
   s.Finish(7,[&](void* p){return os.End(p);},40); }
 { State s;Fake os;Active(s,os);os.startStatus=1;CHECK(!Call(s,os));
   CHECK(s.Get().failed && s.Get().startAttempts==1 && s.Get().starts==0);
   CHECK(!Call(s,os));CHECK(os.startCalls==1);
   s.Finish(7,[&](void* p){return os.End(p);},40);CHECK(os.endCalls==0); }
 { State s;Fake os;Active(s,os);CHECK(Call(s,os));os.endStatus=22;
   s.Control(7,3,false,s.Get().suspensionEpoch,100,[&](void* p){return os.End(p);},40);
   CHECK(s.Get().failed && s.Get().lastEndStatus==22 && s.Get().outstanding==0);
   s.Finish(7,[&](void* p){return os.End(p);},50);CHECK(os.endCalls==1);
   CHECK(s.Get().endAttempts==1 && s.Get().ends==0); }
 // Every external terminal reason uses the same Stop barrier and owner cleanup.
 { State s;Fake os;Active(s,os);CHECK(Call(s,os));
   s.Stop();CHECK(!Call(s,os));
   s.Control(7,4,true,s.Get().suspensionEpoch,100,[&](void* p){return os.End(p);},40);
   s.Finish(7,[&](void* p){return os.End(p);},50);
   CHECK(os.endCalls==1 && s.Get().outstanding==0 && s.Get().terminal); }
 // Deterministic acquire-vs-pause race: OS start is held until pause is ready.
 { State s;Fake os;Active(s,os);std::mutex m;std::condition_variable cv;
   bool entered=false,release=false,pauseReady=false;auto generation=s.Get().generation;
   std::thread host([&]{s.Acquire(generation,42,"PAPlayer",true,[&]{
     std::unique_lock<std::mutex> lock(m);entered=true;cv.notify_all();cv.wait(lock,[&]{return release;});
     return os.Start();},30);});
   {std::unique_lock<std::mutex> lock(m);cv.wait(lock,[&]{return entered;});}
   std::thread owner([&]{ {std::lock_guard<std::mutex> lock(m);pauseReady=true;cv.notify_all();}
     s.Control(7,3,false,s.Get().suspensionEpoch,100,[&](void* p){return os.End(p);},40);});
   {std::unique_lock<std::mutex> lock(m);cv.wait(lock,[&]{return pauseReady;});release=true;cv.notify_all();}
   host.join();owner.join();CHECK(os.startCalls==1 && os.endCalls==1 && s.Get().outstanding==0);
   CHECK(!Call(s,os));s.Finish(7,[&](void* p){return os.End(p);},50);
 }
 // The production owner scope always finishes, including early return and unwinding.
 for(bool throws:{false,true}) { State s;Fake os;Active(s,os);CHECK(Call(s,os));
   try { auto run=[&] { auto cleanup=[&]{s.Finish(7,[&](void* p){return os.End(p);},99);};
     OnExit<decltype(cleanup)> scope(cleanup);if(throws)throw 1;return; };run(); } catch(int) {}
   CHECK(os.endCalls==1 && s.Get().outstanding==0 && !s.Get().ownerRunning);
 }
 { State s;Fake os;Active(s,os);CHECK(Call(s,os));
   s.Finish(42,[&](void* p){return os.End(p);},40); // target cannot end its own token
   CHECK(os.endCalls==0 && s.Get().failed);
   s.Finish(7,[&](void* p){return os.End(p);},50);CHECK(os.endCalls==1);
 }
 if(argc==2) { std::ofstream out(argv[1]);owned_stall::Aggregate metrics;
   for(const auto& snapshot:{capturedActive,capturedClosed}) {
     out<<"{\"kind\":\"clock_sample\",\"host_override\":";owned_override::WriteJson(out,snapshot,snapshot.generation);
     out<<",\"stall_diagnostics\":";owned_stall::WriteJson(out,metrics.Take());out<<"}\n";
   } CHECK(out.good());
 }
 std::ostringstream wire;owned_override::WriteJson(wire,capturedClosed,capturedClosed.generation);
 CHECK(wire.str().size()<2000);
 std::cout<<"host override lifecycle checks passed (NDEBUG-safe)\n";
}
