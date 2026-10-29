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
 std::puts("PASS lifetime: one live owner; quarantine permanently denies restart");
}
