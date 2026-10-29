#if __has_include("OwnedHalAuthorization.h")
#include "OwnedHalAuthorization.h"
#else
#include <cstdio>
int main(){std::fputs("FAIL fresh native authorization absent\n",stderr);return 1;}
#endif
#ifdef OWNED_HAL_AUTHORIZATION_V1
#include <cstdio>
#include <cstdlib>
using namespace owned_hal;
#define CHECK(x) do{if(!(x)){std::fprintf(stderr,"FAIL %d %s\n",__LINE__,#x);std::exit(1);}}while(0)
int main(){
 AuthorizationState s{};s.query=1000000000;s.request=1010000000;s.deadline=1030000000;
 s.start=1011000000;s.end=1012000000;s.callback_entry=1010000000;s.expiry=1500000000;
 s.query_gate=s.before_gate=s.final_gate=1;s.query_intent=s.intent=s.applied_intent=2;s.playing=true;
 CHECK(AuthorizationAllowed(s));s.final_gate=3;CHECK(!AuthorizationAllowed(s));s.final_gate=1;
 s.final_gate=NativeGate::kRetired|1;CHECK(!AuthorizationAllowed(s));s.final_gate=1;
 s.intent=3;CHECK(!AuthorizationAllowed(s));s.intent=2;
 s.end=s.start+5000001;CHECK(!AuthorizationAllowed(s));s.end=s.start+5000000;CHECK(AuthorizationAllowed(s));
 s.callback_entry=s.end-40000001;CHECK(!AuthorizationAllowed(s));s.callback_entry=s.end-40000000;CHECK(AuthorizationAllowed(s));
 s.expiry=s.end;CHECK(!AuthorizationAllowed(s));s.expiry=s.end+1;CHECK(AuthorizationAllowed(s));
 s.source_end=s.end;CHECK(!AuthorizationAllowed(s));s.source_end=s.end+1;CHECK(AuthorizationAllowed(s));
 s.deadline=s.end-1;CHECK(!AuthorizationAllowed(s));s.deadline=s.end;CHECK(AuthorizationAllowed(s));
 std::puts("native authorization fence/time boundaries passed");
}
#endif
