// Fake owner/CoreAudio-free process for real private-file protocol tests.
#include "OwnedHalAuthorization.h"
#include <CommonCrypto/CommonDigest.h>
#include <mach/mach_time.h>
#include <fstream>
#include <thread>
#include <chrono>
using namespace owned_hal;
static uint64_t fixed_now=0;
static uint64_t now(){if(fixed_now)return fixed_now;mach_timebase_info_data_t b{};mach_timebase_info(&b);return static_cast<__uint128_t>(mach_absolute_time())*b.numer/b.denom;}
static std::string hash(const std::string& s){unsigned char d[32];CC_SHA256(s.data(),static_cast<CC_LONG>(s.size()),d);std::string h;for(auto v:d){h+="0123456789abcdef"[v>>4];h+="0123456789abcdef"[v&15];}return h;}
int main(int argc,char** argv){
 if(argc!=3)return 2;const std::string path=argv[1],mode=argv[2];
 std::ofstream started(path+"/started");started<<"started";started.close();
 const uint64_t setup_deadline=now()+1000000000;while(access((path+"/query.json").c_str(),F_OK)!=0&&now()<setup_deadline)std::this_thread::sleep_for(std::chrono::microseconds(200));
 std::ifstream input(path+"/query.json");std::string raw((std::istreambuf_iterator<char>(input)),{});while(!raw.empty()&&raw.back()=='\n')raw.pop_back();
 std::map<std::string,std::string> q;if(!owned_clock::GainJson(raw+"\n",q,4096))return 3;
 HalAuthorization a;if(!a.Open(path+"/authorization",hash)||!a.Remember(raw))return 4;
 auto core=std::make_unique<HalCore>();AppliedControlView view;HalSegment s{};s.sequence=21;s.publication_sequence=2;
 s.control_acquired=s.playing=s.resume_granted=true;s.applied.started=s.applied.sample_zero_proven=1;s.applied_gain=1;
 s.device_generation=s.stream_generation=1;s.playback_epoch=2;s.gain_epoch=1;s.applied_intent=s.applied.effective_intent=2;
 s.native_gate=s.in_flight_generation=1;core->BeginEffectiveIntent(2);s.timebase_numer=s.timebase_denom=1;
 std::ofstream ready(path+"/ready");ready<<"ready";ready.close();
 owned_clock::Transport controls;
 if(mode=="benchmark") {
  std::ifstream seed(path+"/seed-request.json");std::string seed_raw((std::istreambuf_iterator<char>(seed)),{});std::map<std::string,std::string> request;
  if(!owned_clock::GainJson(seed_raw,request,4096))return 9;
  uint64_t request_time=0;owned_clock::GainNumber(request["request_start_ns"],request_time);fixed_now=request_time+1000000;
  s.entry_ticks=fixed_now;s.applied.expiry_ns=fixed_now+500000000;
  for(unsigned i=1;i<=32763;++i){request["request_id"]=std::to_string(i);std::string bytes="{";for(const auto& item:request){if(bytes.size()>1)bytes+=',';bytes+="\""+item.first+"\":"+item.second;}bytes+="}\n";
   char name[80];std::snprintf(name,sizeof(name),"/authorization/request-%06u.json",i);const auto dest=path+name;
   int f=open(dest.c_str(),O_CREAT|O_EXCL|O_WRONLY,0600);if(f<0||write(f,bytes.data(),bytes.size())!=static_cast<ssize_t>(bytes.size()))return 10;close(f);
   bool found=false;if(!a.Poll(found)||!found)return 11;++s.sequence;if(!view.Observe(s)||!a.Respond(view,&s,*core,now,std::string(64,'c')))return 12;
  }
  fixed_now=0;
  if(!controls.Open(path,"{\"benchmark\":true}\n"))return 13;
  std::ofstream populated(path+"/populated");populated<<"32763";populated.close();
  const uint64_t until=now()+5000000000;
  while(access((path+"/final-query.json").c_str(),F_OK)!=0&&now()<until)std::this_thread::sleep_for(std::chrono::milliseconds(1));
  std::ifstream final_input(path+"/final-query.json");std::string final((std::istreambuf_iterator<char>(final_input)),{});while(!final.empty()&&final.back()=='\n')final.pop_back();
  if(!a.Remember(final))return 14;
 }
 const uint64_t stop=now()+2000000000;unsigned count=0;uint64_t next_gain=0,gain_revision=5999;
 std::ofstream cadence;if(mode=="benchmark")cadence.open(path+"/native-cadence.jsonl");
 while(now()<stop){
  if(mode=="benchmark") {
   const auto start=now();std::vector<std::string> commands;if(!controls.Poll(64,commands))return 15;
   const auto after_control=now();uint64_t gain_end=after_control;
   uint64_t gain_pending=0;if(start>=next_gain){std::vector<owned_clock::Gain> pending;if(!controls.PollGainNext(gain_revision,pending))return 16;if(!pending.empty())gain_revision=pending.front().revision;gain_pending=pending.size();gain_end=now();next_gain=start+20000000;}
   cadence<<"{\"start_ns\":"<<start<<",\"control_end_ns\":"<<after_control<<",\"gain_end_ns\":"<<gain_end<<",\"gain_pending\":"<<gain_pending<<"}\n";cadence.flush();
  }
  bool found=false;if(!a.Poll(found))return 5;
  if(found){s.entry_ticks=now();s.applied.expiry_ns=s.entry_ticks+500000000;++s.sequence;if(!view.Observe(s))return 6;
   if(mode=="pause")core->NativeSpeed(0);if(mode=="route")core->Retire(Reason::route);
   if(mode=="aba"){core->NativeSpeed(0);core->NativeSpeed(1000);}
   if(mode=="intent")core->BeginEffectiveIntent(3);
   if(!a.Respond(view,&s,*core,now,std::string(64,'c')))return 7;
   if(++count==((mode=="bridge"||mode=="benchmark")?5:(mode=="stages"?3:1)))return 0;
  }
  std::this_thread::sleep_for(std::chrono::microseconds(mode=="benchmark"?5000:200));
 }
 return 8;
}
