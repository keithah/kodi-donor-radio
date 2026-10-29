#include "OwnedHalLifetime.h"
#include <cstdio>
#include <cstdlib>
#include <thread>
using namespace owned_hal;
#define CHECK(x) do{if(!(x)){std::fprintf(stderr,"FAIL %d %s\n",__LINE__,#x);std::exit(1);}}while(0)
int main(){
 LifetimeGate gate;
 CHECK(gate.Claim());CHECK(!gate.Claim());gate.Release();CHECK(gate.Claim());
 gate.Quarantine();gate.Release();CHECK(gate.IsQuarantined());CHECK(!gate.Claim());
 LifetimeGate concurrent;std::atomic<unsigned> wins{0};std::thread a([&]{if(concurrent.Claim())++wins;});
 std::thread b([&]{if(concurrent.Claim())++wins;});a.join();b.join();CHECK(wins==1);
 // Deterministic barriers expose the interval between terminal choice and
 // gate release. This releasable case represents a never-exposed context.
 LifetimeGate clean_gate;CleanupArbiter clean;CHECK(clean_gate.Claim());CleanupResult observed=CleanupResult::failed;
 std::atomic<bool> allow_choice{false},chosen{false},allow_release{false},released{false};
 std::thread cleaner([&]{while(!allow_choice.load())std::this_thread::yield();
   CHECK(clean.Choose(CleanupResult::clean));chosen.store(true);
   while(!allow_release.load())std::this_thread::yield();clean_gate.Release();released.store(true);});
 CHECK(!clean.Result(observed)); // deterministic stale pending observation
 allow_choice.store(true);while(!chosen.load())std::this_thread::yield();
 CHECK(!clean_gate.Claim());CHECK(!clean.Choose(CleanupResult::timeout_quarantined));
 allow_release.store(true);while(!released.load())std::this_thread::yield();
 CHECK(clean_gate.Claim()); // competing second session admitted
 if(clean.Choose(CleanupResult::timeout_quarantined))clean_gate.Quarantine();
 CHECK(!clean_gate.IsQuarantined());CHECK(clean.Result(observed)&&observed==CleanupResult::clean);
 cleaner.join();clean_gate.Release();
 // Timeout wins while the worker is stopped before its terminal transition.
 LifetimeGate timeout_gate;CleanupArbiter timeout;CHECK(timeout_gate.Claim());
 std::atomic<bool> timeout_chosen{false};
 std::thread late_worker([&]{while(!timeout_chosen.load())std::this_thread::yield();
   if(timeout.Choose(CleanupResult::clean))timeout_gate.Release();});
 CHECK(timeout.Choose(CleanupResult::timeout_quarantined));timeout_gate.Quarantine();
 CHECK(!timeout_gate.Claim());timeout_chosen.store(true);late_worker.join();CHECK(!timeout_gate.Claim());
 CHECK(timeout.Result(observed)&&observed==CleanupResult::timeout_quarantined);
 CHECK(!timeout.Choose(CleanupResult::failed));CHECK(timeout.Result(observed)&&observed==CleanupResult::timeout_quarantined);
 // Even a clean winner is retained after exposure; a stale timeout cannot
 // change its result and a competing session can never claim that gate.
 LifetimeGate exposed_gate;CleanupArbiter exposed;CHECK(exposed_gate.Claim());
 CHECK(exposed.Choose(CleanupResult::clean));exposed_gate.Quarantine();
 CHECK(!exposed_gate.Claim());CHECK(!exposed.Choose(CleanupResult::timeout_quarantined));
 CHECK(exposed.Result(observed)&&observed==CleanupResult::clean);
 std::puts("PASS lifetime: deterministic clean/timeout arbitration, competing owner, retained exposed cleanup");
}
