// Actual StreamManager::Open/DemuxRead with real FFmpeg/Timeshift and fake SDK/OS tables.
#include "StreamManager.h"
#include "OwnedHalDevice.h"
#include <kodi/c-api/filesystem.h>
#include <kodi/c-api/network.h>
#include <mach/mach_time.h>
#include <pthread/qos.h>
#include <sys/stat.h>
#include <unistd.h>
#include <fcntl.h>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <fstream>
#include <sstream>
#include <iomanip>
#include <thread>
#include <chrono>
#include <atomic>
#include <vector>
#include <map>
#define CHECK(x) do{if(!(x)){std::fprintf(stderr,"FAIL %d %s\n",__LINE__,#x);std::exit(1);}}while(0)
static std::atomic<unsigned> priority_stub_calls{0},override_stub_calls{0};
extern "C" int pthread_set_qos_class_self_np(qos_class_t,int){++priority_stub_calls;return 0;}
extern "C" pthread_override_t pthread_override_qos_class_start_np(pthread_t,qos_class_t,int){++override_stub_calls;return nullptr;}
extern "C" int pthread_override_qos_class_end_np(pthread_override_t){++override_stub_calls;return 0;}
namespace {
uint64_t launch_ns=0;
std::string output,session,nonce="e1e1e1e1e1e1e1e1e1e1e1e1e1e1e1e1",incarnation;
std::atomic<unsigned> guard_entries{0},guard_release{0},guard_entered{0};
std::atomic<uint64_t> guard_entered_ns{0};
AudioDeviceIOProc callback=nullptr;void* context=nullptr;std::atomic<unsigned> starts{0},destroys{0},made_ae{0};
std::atomic<bool> route_bad{false},early_abort_fired{false};std::atomic<uint64_t> stop_ns{0},destroy_ns{0};
InputStreamFFmpegDirect* current_stream=nullptr;std::string current_scenario;
uint64_t now()noexcept{mach_timebase_info_data_t t{};mach_timebase_info(&t);return uint64_t((__uint128_t)mach_absolute_time()*t.numer/t.denom);}
uint64_t ticks()noexcept{return mach_absolute_time();}
void wait_tick()noexcept{std::this_thread::sleep_for(std::chrono::milliseconds(1));}
owned_hal::Status pin()noexcept{return owned_hal::Status::ok;}
OSStatus resolve(owned_hal::DeviceContract& c)noexcept{mach_timebase_info_data_t t{};mach_timebase_info(&t);c.device=1;c.stream=2;c.timebase_numer=t.numer;c.timebase_denom=t.denom;return 0;}
bool read_race(){return current_scenario=="gain_read_race"||current_scenario=="playback_read_race"||current_scenario=="gain_future"||current_scenario=="gain_expired"||current_scenario=="gain_stale"||current_scenario=="playback_future"||current_scenario=="playback_stale";}
bool playback_race(){return current_scenario.compare(0,9,"playback_")==0;}
void await_stage(std::atomic<unsigned>& value,unsigned stage){const auto deadline=now()+3000000000ULL;while(value.load()<stage&&now()<deadline)wait_tick();CHECK(value.load()>=stage);}
OSStatus read_guard(const owned_hal::DeviceContract&)noexcept{
 if(read_race()&&starts.load()){
  const unsigned stage=++guard_entries;
  const unsigned target=playback_race()?4:2;
  if(stage==1||stage==target){guard_entered_ns=now();guard_entered=stage;await_stage(guard_release,stage);}
 }
 return route_bad.load()?-1:0;
}
OSStatus listener(const owned_hal::DeviceContract&,uint32_t,AudioObjectPropertyListenerProc,void*)noexcept{return 0;}
OSStatus create(AudioDeviceID,AudioDeviceIOProc p,void* ctx,AudioDeviceIOProcID* id)noexcept{callback=p;context=ctx;if(current_scenario=="cancel_open"){std::thread cancel([]{current_stream->DemuxAbort();});cancel.join();}*id=reinterpret_cast<AudioDeviceIOProcID>(uintptr_t(1));return 0;}
OSStatus start(AudioDeviceID,AudioDeviceIOProcID)noexcept{++starts;return 0;}
OSStatus stop(AudioDeviceID,AudioDeviceIOProcID)noexcept{stop_ns=now();return 0;}
OSStatus destroy(AudioDeviceID,AudioDeviceIOProcID)noexcept{destroy_ns=now();++destroys;return 0;}
owned_hal::DeviceApi api(){owned_hal::DeviceApi a{};a.pin=pin;a.now_ns=now;a.ticks=ticks;a.wait_tick=wait_tick;a.resolve=resolve;a.read=read_guard;a.add=a.remove=listener;a.create=create;a.start=start;a.stop=stop;a.destroy=destroy;return a;}
std::string path(const char* value){std::string p(value);CHECK(p.compare(0,output.size(),output)==0);return p;}
DEMUX_PACKET* allocate(void*,int bytes){auto* p=new DEMUX_PACKET{};if(bytes)p->pData=static_cast<uint8_t*>(std::calloc(size_t(bytes)+64,1));p->iSize=bytes;return p;}
void release(void*,DEMUX_PACKET* p){if(p){std::free(p->pData);delete p;}}
void setup(){static AddonGlobalInterface global{};static AddonToKodiFuncTable_Addon to{};static AddonToKodiFuncTable_kodi_addon addon{};static AddonToKodiFuncTable_kodi_filesystem fs{};static AddonToKodiFuncTable_kodi_audioengine ae{};
 static AddonToKodiFuncTable_kodi_network network{};network.get_user_agent=[](void*){return strdup("KodiHALIntegration/1");};to.kodi_network=&network;
 global.toKodi=&to;to.kodi_addon=&addon;to.kodi_filesystem=&fs;to.kodi_audioengine=&ae;to.addon_log_msg=[](void*,int level,const char* msg){(void)level;std::fprintf(stderr,"sdk: %s\n",msg);
 if((current_scenario=="cancel_before_selection"||current_scenario=="ae_before_selection")&&std::strstr(msg,"Num Props")&&!early_abort_fired.exchange(true)){std::thread cancel([]{current_stream->DemuxAbort();});cancel.join();}};to.free_string=[](void*,char* s){std::free(s);};
 addon.get_setting_bool=[](void*,const char*,bool* v){*v=false;return true;};addon.get_setting_int=[](void*,const char*,int* v){*v=0;return true;};addon.get_setting_float=[](void*,const char*,float* v){*v=0;return true;};addon.get_setting_string=[](void*,const char* key,char** v){*v=strdup(std::string(key)=="timeshiftBufferPath"?(output+"/timeshift").c_str():"");return true;};
 fs.get_cookies=[](void*,const char*,char**){return false;};
 fs.directory_exists=[](void*,const char* p){struct stat s{};return ::stat(path(p).c_str(),&s)==0;};fs.create_directory=[](void*,const char* p){return ::mkdir(path(p).c_str(),0700)==0;};fs.file_exists=[](void*,const char* p,bool){return ::access(path(p).c_str(),F_OK)==0;};fs.delete_file=[](void*,const char* p){if(!p||!*p)return false;return ::unlink(path(p).c_str())==0;};
 fs.open_file=[](void*,const char* p,unsigned)->void*{return std::fopen(path(p).c_str(),"rb");};fs.open_file_for_write=[](void*,const char* p,bool)->void*{return std::fopen(path(p).c_str(),"w+b");};fs.read_file=[](void*,void* f,void* b,size_t n)->ssize_t{return std::fread(b,1,n,static_cast<FILE*>(f));};fs.write_file=[](void*,void* f,const void* b,size_t n)->ssize_t{return std::fwrite(b,1,n,static_cast<FILE*>(f));};fs.seek_file=[](void*,void* f,int64_t n,int whence)->int64_t{fseeko(static_cast<FILE*>(f),n,whence);return ftello(static_cast<FILE*>(f));};fs.close_file=[](void*,void* f){std::fclose(static_cast<FILE*>(f));};fs.get_file_length=[](void*,void* f)->int64_t{struct stat s{};fstat(fileno(static_cast<FILE*>(f)),&s);return s.st_size;};
 ae.make_stream=[](void*,AUDIO_ENGINE_FORMAT*,unsigned)->AEStreamHandle*{++made_ae;return reinterpret_cast<AEStreamHandle*>(uintptr_t(1));};
 ae.free_stream=[](void*,AEStreamHandle*){};
 ae.aestream_get_space=[](void*,AEStreamHandle*)->unsigned{return 4096;};
 ae.aestream_get_delay=ae.aestream_get_cache_time=ae.aestream_get_cache_total=[](void*,AEStreamHandle*){return 0.0;};
 ae.aestream_is_buffering=ae.aestream_is_draining=ae.aestream_is_drained=[](void*,AEStreamHandle*){return false;};
 ae.aestream_pause=ae.aestream_resume=ae.aestream_flush=[](void*,AEStreamHandle*){};
 ae.aestream_drain=[](void*,AEStreamHandle*,bool){};
 kodi::addon::CPrivateBase::m_interface=&global;
}
void write(const std::string& name,const std::string& value){std::string dest=session+"/"+name,temp=session+"/."+name+".tmp";FILE* f=std::fopen(temp.c_str(),"wx");CHECK(f);CHECK(fchmod(fileno(f),0600)==0);CHECK(std::fwrite(value.data(),1,value.size(),f)==value.size());std::fclose(f);CHECK(renameatx_np(AT_FDCWD,temp.c_str(),AT_FDCWD,dest.c_str(),RENAME_EXCL)==0);}
std::string contents(const std::string& name){std::ifstream f(session+"/"+name);return std::string(std::istreambuf_iterator<char>(f),{});}
void command(unsigned revision,const char* state){write("command-00000"+std::to_string(revision)+".txt","v1 "+nonce+" "+incarnation+" "+std::to_string(revision)+" "+state+" "+std::to_string(revision==1?launch_ns:now())+"\n");}
}
int main(int argc,char** argv){CHECK(argc==4);output=argv[1];session=argv[2];std::string scenario=argv[3];CHECK(mkdir(output.c_str(),0700)==0);setup();
 KODI_ADDON_INSTANCE_FUNC funcs{};KODI_ADDON_INSTANCE_INFO info{};info.version="2.0.12";info.id="integration";info.type=ADDON_INSTANCE_INPUTSTREAM;AddonToKodiFuncTable_InputStream to{};KodiToAddonFuncTable_InputStream from{};AddonInstance_InputStream data{};data.toKodi=&to;data.toAddon=&from;to.allocate_demux_packet=allocate;to.free_demux_packet=release;KODI_ADDON_INSTANCE_STRUCT instance{};instance.info=&info;instance.functions=&funcs;instance.inputstream=&data;
 kodi::addon::IInstanceInfo ii(&instance);InputStreamFFmpegDirect stream(ii,api());current_stream=&stream;current_scenario=scenario;
 INPUTSTREAM_PROPERTY props{};props.m_strURL="http://127.0.0.1:18789/live/index.m3u8";props.m_mimeType="application/vnd.apple.mpegurl";std::map<std::string,std::string> properties{{"stream_mode","timeshift"},{"terminal_eof","true"},{"is_realtime_stream","false"},{"owned_clock_nonce",nonce},{"owned_clock_timeline",std::string(64,'c')},{"owned_clock_first_packet","6e2b7fc4141143ff51923f0fc26127ebecfd7eb035c2d542598544973f8fd702"},{"owned_player_mode","videoplayer_video_v1"},{"owned_player_core","VideoPlayer"},{"owned_player_queue_tenths","40"},{"owned_output_backend",scenario=="bad_backend"?"foreign":"hal_callback_v1"},{"owned_hal_contract_version","1"}};if(scenario=="ae"||scenario=="ae_before_selection"){properties["owned_output_backend"]="ae";properties.erase("owned_hal_contract_version");}
 std::vector<std::string> keys;for(auto& kv:properties)keys.push_back("inputstream.ffmpegdirect."+kv.first);size_t n=0;for(auto& kv:properties){props.m_ListItemProperties[n]={keys[n].c_str(),kv.second.c_str()};++n;}props.m_nCountInfoValues=n;
 if(scenario=="bad_backend"){CHECK(!from.open(&data,&props));CHECK(!callback&&!made_ae);std::puts("PASS backend mismatch before source/device open");return 0;}
 launch_ns=now();
 if(scenario=="cancel_before_selection"||scenario=="ae_before_selection"){CHECK(!from.open(&data,&props));CHECK(early_abort_fired&&starts==0&&made_ae==0&&!callback&&!owned_hal::Quarantined());std::puts("PASS concurrent abort before backend selection, no pointer access, registration or source start");return 0;}
 if(scenario=="cancel_open"){CHECK(!from.open(&data,&props));CHECK(starts==0&&destroys==1&&owned_hal::Quarantined());CHECK(priority_stub_calls==0);std::puts("PASS cancellation during actual Open registration, retained exposed cleanup");return 0;}
 CHECK(from.open(&data,&props));
 if(scenario=="ae"){CHECK(!callback&&starts==0&&made_ae==1);}else CHECK(callback&&starts==1&&made_ae==0);
 auto hello=contents("hello.json");auto pos=hello.find("\"incarnation\":\"");CHECK(pos!=std::string::npos);incarnation=hello.substr(pos+15,32);
 if(scenario!="ae")CHECK(hello.find("hal_callback_v1")!=std::string::npos);
 // Freshness regression deliberately precedes DemuxRead/source transfer. The
 // fake read barrier sits after the real owner's loop timestamp; all PCM stays zero.
 if(read_race()){
   auto bootstrapGain=[&]{const uint64_t issued=now()+(scenario=="gain_future"?1000000000ULL:0);
     const std::string wire="{\"nonce\":\""+nonce+"\",\"incarnation\":\""+incarnation+"\",\"timeline_sha256\":\""+std::string(64,'c')+
       "\",\"output_backend\":\"hal_callback_v1\",\"hal_contract_version\":1,\"device_generation\":1,\"revision\":1,\"effective_epoch\":1,\"request_start_ns\":"+std::to_string(issued)+
       ",\"request_end_ns\":"+std::to_string(issued)+",\"issue_ns\":"+std::to_string(issued)+",\"expiry_ns\":"+std::to_string(issued+500000000)+",\"volume\":100,\"muted\":false,\"multiplier\":1}\n";
     write("gain-000001.json",wire);return issued;
   };
   await_stage(guard_entered,1);command(1,"waiting");
   if(playback_race())bootstrapGain();
   guard_release=1;const unsigned target=playback_race()?4:2;await_stage(guard_entered,target);
   const uint64_t entered=guard_entered_ns.load();uint64_t issued=0;
   if(!playback_race())issued=bootstrapGain();
   else {
     issued=scenario=="playback_stale"?launch_ns:now()+(scenario=="playback_future"?1000000000ULL:0);
     write("command-000002.txt","v1 "+nonce+" "+incarnation+" 2 stop "+std::to_string(issued)+"\n");
   }
   if(scenario!="playback_stale")CHECK(issued>entered);
   if(scenario=="gain_expired")while(now()<=issued+500000000ULL)wait_tick();
   guard_release=target;
   std::fprintf(stderr,"barrier: %s guard entered %llu; command issued %llu; complete atomic publication before guard returns\n",scenario.c_str(),(unsigned long long)entered,(unsigned long long)issued);
   float samples[1024]{};AudioBufferList buffers{};buffers.mNumberBuffers=1;buffers.mBuffers[0]={2,4096,samples};
   AudioTimeStamp stamp{};stamp.mFlags=3;mach_timebase_info_data_t tb{};mach_timebase_info(&tb);const uint64_t base=ticks();
   for(unsigned n=0;n<8&&!destroys.load();++n){
     stamp.mSampleTime=n*512;stamp.mHostTime=base+uint64_t((__uint128_t)(40000000ULL+uint64_t(n)*512*1000000000/48000)*tb.denom/tb.numer);
     AudioTimeStamp current=stamp;current.mHostTime=ticks();CHECK(callback(1,&current,&buffers,&current,&buffers,&stamp,context)==0);
     for(float v:samples)CHECK(v==0);std::this_thread::sleep_for(std::chrono::milliseconds(10));
   }
   const auto evidence=contents("native.jsonl");
   if(scenario=="gain_read_race"||scenario=="gain_stale"){
     CHECK(destroys==0);CHECK(evidence.find("hal_gain_ack")!=std::string::npos);CHECK(evidence.find("hal_terminal")==std::string::npos);
     if(scenario=="gain_stale"){
       auto stale=contents("gain-000001.json");const auto at=stale.find("\"revision\":1");CHECK(at!=std::string::npos);stale.replace(at,12,"\"revision\":2");write("gain-000002.json",stale);
       for(unsigned n=0;n<100&&!destroys.load();++n)wait_tick();CHECK(destroys==1);
       CHECK(contents("native.jsonl").find("\"native_control_reason\":\"gain_command\"")!=std::string::npos);
     }
   }else {
     CHECK(destroys==1);const std::string reason=scenario=="playback_read_race"?"control_stop":playback_race()?"command_invalid":"gain_command";
     CHECK(evidence.find("\"native_control_reason\":\""+reason+"\"")!=std::string::npos);
   }
   from.demux_abort(&data);from.close(&data);CHECK(destroys==1&&owned_hal::Quarantined());
   std::puts("PASS actual Open owner/transport post-read freshness, zero source consumption and retained cleanup");return 0;
 }
 CHECK(pthread_setname_np("VideoPlayer")==0);DEMUX_PACKET* packet=nullptr;for(unsigned tries=0;tries<1000;++tries){packet=from.demux_read(&data);if(packet&&packet->iSize)break;if(packet)release(nullptr,packet);packet=nullptr;std::this_thread::sleep_for(std::chrono::milliseconds(1));}CHECK(packet&&packet->iSize==384);release(nullptr,packet);
 INPUTSTREAM_IDS ids{};CHECK(from.get_stream_ids(&data,&ids)&&ids.m_streamCount==1);INPUTSTREAM_INFO host{};KODI_HANDLE transferred=nullptr;CHECK(from.get_stream(&data,ids.m_streamIds[0],&host,&transferred,[](KODI_HANDLE,int,INPUTSTREAM_INFO* info)->KODI_HANDLE{CHECK(info->m_streamType==INPUTSTREAM_TYPE_VIDEO&&info->m_ExtraSize==1&&info->m_ExtraData[0]==0&&info->m_Width==16&&info->m_Height==16);return reinterpret_cast<void*>(uintptr_t(1));}));CHECK(transferred);
 if(scenario=="ae"){from.demux_abort(&data);from.close(&data);CHECK(priority_stub_calls==1&&override_stub_calls==0);CHECK(!owned_hal::Quarantined()&&made_ae==1&&starts==0);std::puts("PASS actual AE Open/DemuxRead uses fake AE only; priority stub called once, no OS scheduling call");return 0;}
 CHECK(priority_stub_calls==0&&override_stub_calls==0);
 // Source is prefilled, yet no callback may consume until the monitor play acknowledgement.
 float samples[1024]{};AudioBufferList buffers{};buffers.mNumberBuffers=1;buffers.mBuffers[0]={2,4096,samples};AudioTimeStamp stamp{};stamp.mFlags=3;mach_timebase_info_data_t tb{};mach_timebase_info(&tb);const uint64_t callbackBase=ticks();const uint64_t outputBase=callbackBase+uint64_t((__uint128_t)40000000*tb.denom/tb.numer);
 stamp.mHostTime=outputBase;stamp.mSampleTime=0;AudioTimeStamp current=stamp;current.mHostTime=ticks();CHECK(callback(1,&current,&buffers,&current,&buffers,&stamp,context)==0);for(float v:samples)CHECK(v==0);
 // Cancellation/cleanup exercises the actual Open source-owner/decoder teardown.
 if(scenario=="cancel"){from.demux_abort(&data);from.close(&data);CHECK(destroys==1);CHECK(owned_hal::Quarantined());std::puts("PASS actual Open/DemuxRead sample-zero prefill, host caps, cancellation, retained cleanup");return 0;}
 command(1,"waiting");command(2,"play"); // fake SDK callback acknowledgement; live Kodi onAVStarted remains controller gate

 uint64_t gainRevision=0,gainEpoch=0,lastGain=0;unsigned volume=100,previousVolume=101;bool muted=false,previousMute=false;
 auto publishGain=[&]{const uint64_t requested=now();if(volume!=previousVolume||muted!=previousMute)++gainEpoch;
   owned_clock::Gain gain;const std::string wireNonce=scenario=="foreign_gain"&&gainRevision>=6?std::string(32,'a'):nonce;std::ostringstream wire;wire<<std::setprecision(17)<<"{\"nonce\":\""<<wireNonce<<"\",\"incarnation\":\""<<incarnation<<"\",\"timeline_sha256\":\""<<std::string(64,'c')
    <<"\",\"output_backend\":\"hal_callback_v1\",\"hal_contract_version\":1,\"device_generation\":1,\"revision\":"<<++gainRevision<<",\"effective_epoch\":"<<gainEpoch
    <<",\"request_start_ns\":"<<requested<<",\"request_end_ns\":"<<requested<<",\"issue_ns\":"<<requested<<",\"expiry_ns\":"<<requested+500000000
    <<",\"volume\":"<<volume<<",\"muted\":"<<(muted?"true":"false")<<",\"multiplier\":"<<owned_hal::KodiGain(volume,muted)<<"}\n";
   CHECK(owned_clock::ParseGain(wire.str(),gain));char name[32];std::snprintf(name,sizeof(name),"gain-%06llu.json",static_cast<unsigned long long>(gainRevision));write(name,wire.str());lastGain=requested;previousVolume=volume;previousMute=muted;};
 publishGain();std::ofstream rendered(output+"/rendered.f32",std::ios::binary);rendered.write(reinterpret_cast<char*>(samples),4096);
 unsigned controlStage=0;uint64_t callbacks=1;
 for(;callbacks<450&&!destroys.load();++callbacks){
   const uint64_t due=callbackBase+uint64_t((__uint128_t)callbacks*512*1000000000*tb.denom/(48000*tb.numer));
   while(ticks()<due)std::this_thread::sleep_for(std::chrono::microseconds(100));
   const double elapsed=double(callbacks)*512/48000;
   if(controlStage==0&&elapsed>=.3){from.demux_set_speed(&data,0);command(3,"pause");++controlStage;}
   if(controlStage==1&&elapsed>=.5){command(4,"resume");from.demux_set_speed(&data,1000);++controlStage;}
   if(elapsed>=.6&&elapsed<.8)volume=90;else if(elapsed>=.8&&elapsed<1.4)volume=75;else if(elapsed>=1.4&&elapsed<1.6)volume=0;else volume=100;
   muted=elapsed>=1.0&&elapsed<1.2;
   if(scenario=="route"&&elapsed>=.7)route_bad=true;
   const bool renew=(scenario!="lease"||elapsed<.2)&&(scenario!="eof_stale"||elapsed<2.5);
   if(renew&&(now()-lastGain>=100000000||volume!=previousVolume||muted!=previousMute))publishGain();
   std::memset(samples,0,sizeof(samples));stamp.mSampleTime=callbacks*512;stamp.mHostTime=outputBase+uint64_t((__uint128_t)callbacks*512*1000000000*tb.denom/(48000*tb.numer));current.mHostTime=ticks();
   CHECK(callback(1,&current,&buffers,&current,&buffers,&stamp,context)==0);rendered.write(reinterpret_cast<char*>(samples),4096);
   auto* hostPacket=from.demux_read(&data);if(hostPacket)release(nullptr,hostPacket);
 }
 rendered.close();CHECK(destroys==1);CHECK(owned_hal::Quarantined());
 const bool expectedNatural=scenario=="eof"||scenario=="eof_stale";
 CHECK((contents("native.jsonl").find("natural_eof")!=std::string::npos)==expectedNatural);
 std::ofstream stats(output+"/native-result.json");stats<<"{\"stop_ns\":"<<stop_ns.load()<<",\"destroy_ns\":"<<destroy_ns.load()<<",\"natural_eof_expected\":"<<(expectedNatural?"true":"false")<<"}";stats.close();
 from.close(&data);CHECK(destroys==1);std::puts("PASS actual HAL source, normal controls, source END hold and retained cleanup");

}
