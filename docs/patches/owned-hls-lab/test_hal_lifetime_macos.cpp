#include <dlfcn.h>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <thread>
#include <array>
#include <atomic>
#define CHECK(x) do{if(!(x)){std::fprintf(stderr,"FAIL %d %s\n",__LINE__,#x);std::exit(1);}}while(0)
int main(int argc,char** argv){
 CHECK(argc==2||argc==3);void* module=dlopen(argv[1],RTLD_NOW|RTLD_LOCAL);CHECK(module);
 if(argc==3&&std::strcmp(argv[2],"unpin")==0){CHECK(dlclose(module)==0);CHECK(!dlopen(argv[1],RTLD_NOW|RTLD_LOCAL|RTLD_NOLOAD));std::puts("PASS unpinned control: ordinary dlclose really unloads this test image");return 0;}
 auto callback=reinterpret_cast<int(*)()>(dlsym(module,"retained_callback"));
 auto pin=reinterpret_cast<int(*)()>(dlsym(module,"pin_module"));
 auto count=reinterpret_cast<unsigned(*)()>(dlsym(module,"pin_count"));CHECK(callback&&pin&&count);
 auto start=reinterpret_cast<int(*)()>(dlsym(module,"start_fake"));
 auto drop=reinterpret_cast<int(*)()>(dlsym(module,"drop_owner"));
 auto clean=reinterpret_cast<int(*)()>(dlsym(module,"clean_owner"));CHECK(clean);
 auto release=reinterpret_cast<void(*)()>(dlsym(module,"release_fake_cleanup"));CHECK(start&&drop&&release);
 if(argc==3&&std::strcmp(argv[2],"mismatch")==0){CHECK(pin()==0);CHECK(pin()==0);CHECK(count()==0);CHECK(dlclose(module)==0);CHECK(!dlopen(argv[1],RTLD_NOW|RTLD_LOCAL|RTLD_NOLOAD));std::puts("PASS identity mismatch: fail closed without leaking a loader reference");return 0;}
 CHECK(callback()==1);CHECK(count()==0);std::array<std::thread,8> threads;std::atomic<bool> pins_ok{true};
 for(auto& thread:threads)thread=std::thread([&]{if(pin()!=1)pins_ok.store(false);});
 for(auto& thread:threads)thread.join();CHECK(pins_ok);CHECK(pin()==1);CHECK(count()==1);
 Dl_info before{},after{};CHECK(dladdr(reinterpret_cast<void*>(callback),&before));
 CHECK(start()==1);void* base=before.dli_fbase;CHECK(dlclose(module)==0); // ordinary owner is gone
 CHECK(dladdr(reinterpret_cast<void*>(callback),&after));CHECK(after.dli_fbase==base);
 CHECK(callback()==2);CHECK(callback()==3);CHECK(count()==1);
 void* probe=dlopen(argv[1],RTLD_NOW|RTLD_LOCAL|RTLD_NOLOAD);CHECK(probe);CHECK(dlclose(probe)==0);
 if(argc==3&&std::strcmp(argv[2],"retained-clean")==0)CHECK(clean()==1);
 else CHECK(drop()==1);
 CHECK(callback()==4);release();CHECK(callback()==5);
 if(argc==3&&std::strcmp(argv[2],"retained-clean")==0)
  std::puts("PASS actual dyld: successful cleanup + ordinary dlclose + facade destruction retain delayed callback/context, one self-pin");
 else std::puts("PASS actual dyld: ordinary dlclose + facade destruction + hung cleanup retain actual callback/context, matching image, one self-pin");
}
