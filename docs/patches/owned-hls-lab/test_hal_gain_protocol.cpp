#include "OwnedClockProtocol.h"
#include "OwnedClockTransport.h"
#include <cstdio>
#include <cstdlib>
#include <sys/stat.h>
using namespace owned_clock;
#define CHECK(x) do{if(!(x)){std::fprintf(stderr,"FAIL %d %s\n",__LINE__,#x);std::exit(1);}}while(0)
std::string replace(std::string wire,const std::string& from,const std::string& to){auto p=wire.find(from);CHECK(p!=std::string::npos);wire.replace(p,from.size(),to);return wire;}
int main(){const std::string good="{\"nonce\":\""+std::string(32,'a')+"\",\"incarnation\":\""+std::string(32,'b')+"\",\"timeline_sha256\":\""+std::string(64,'c')+"\",\"device_generation\":1,\"revision\":1,\"effective_epoch\":1,\"request_start_ns\":1000000000,\"request_end_ns\":1050000000,\"issue_ns\":1050000000,\"expiry_ns\":1500000000,\"volume\":90,\"muted\":false,\"multiplier\":0.5011872336272722,\"output_backend\":\"hal_callback_v1\",\"hal_contract_version\":1}\n";Gain parsed;CHECK(ParseGain(good,parsed));CHECK(parsed.revision==1&&parsed.volume==90&&parsed.end-parsed.start==50000000);
for(auto wire:{replace(good,"\"revision\":1","\"revision\":true"),replace(good,"\"volume\":90","\"volume\":90.0"),replace(good,"\"muted\":false","\"muted\":0"),replace(good,"0.5011872336272722","NaN"),replace(good,"0.5011872336272722","1e9999"),replace(good,"0.5011872336272722","0x1p0"),replace(good,"1050000000","1050000001"),replace(good,"1500000000","1500000001"),replace(good,"\"revision\":1","\"revision\":01"),replace(good,"\"revision\":1","\"revision\":1,\"revision\":1"),replace(good,"hal_callback_v1","ae"),replace(good,"\"device_generation\":1","\"device_generation\":0")})CHECK(!ParseGain(wire,parsed));
char root[]="/private/tmp/hal-gain-protocol-XXXXXX";CHECK(mkdtemp(root));CHECK(chmod(root,0700)==0);
auto save=[&](const char* name,const std::string& value){std::string path=std::string(root)+"/"+name;FILE* f=std::fopen(path.c_str(),"w");CHECK(f);CHECK(fchmod(fileno(f),0600)==0);CHECK(std::fwrite(value.data(),1,value.size(),f)==value.size());std::fclose(f);};
Transport transport;CHECK(transport.Open(root,"{}\n"));std::vector<Gain> pending;CHECK(transport.PollGain(0,pending)&&pending.empty());save("gain-000001.json",good);CHECK(transport.PollGain(0,pending)&&pending.size()==1);CHECK(transport.PollGain(1,pending)&&pending.empty());
save("gain-000003.json",replace(good,"\"revision\":1","\"revision\":3"));CHECK(!transport.PollGain(1,pending));CHECK(unlink((std::string(root)+"/gain-000003.json").c_str())==0);
save("gain-000001.json",replace(good,"\"volume\":90","\"volume\":75"));CHECK(!transport.PollGain(1,pending));
std::puts("PASS strict HAL gain JSON, exact brackets/lease, immutable pinned sequential namespace");}
