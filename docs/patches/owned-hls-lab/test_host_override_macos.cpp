// Optional macOS API check. Only this process's own worker receives one token.
#include "OwnedHostOverride.h"
#include <pthread.h>
#include <pthread/qos.h>
#include <cerrno>
#include <condition_variable>
#include <cstdlib>
#include <iostream>
#include <mutex>
#include <thread>
#define CHECK(x) do { if(!(x)) { std::cerr<<__LINE__<<": "<<#x<<"\n";std::exit(1); } } while(false)
uint64_t Identity() { uint64_t id=0;CHECK(pthread_threadid_np(nullptr,&id)==0);return id; }
int main() {
  owned_override::State state;const auto owner=Identity();CHECK(state.Reset(true));CHECK(state.OwnerBegin(owner));
  auto end=[](void* p){return pthread_override_qos_class_end_np(static_cast<pthread_override_t>(p));};
  state.Control(owner,2,true,end,1);
  std::mutex mutex;std::condition_variable cv;bool acquired=false,done=false;
  qos_class_t before=QOS_CLASS_UNSPECIFIED,during=QOS_CLASS_UNSPECIFIED,after=QOS_CLASS_UNSPECIFIED;
  int beforeRelative=0,duringRelative=0,afterRelative=0;
  std::thread worker([&] {
    CHECK(pthread_setname_np("PAPlayer")==0);
    CHECK(pthread_get_qos_class_np(pthread_self(),&before,&beforeRelative)==0);
    CHECK(state.Acquire(state.Get().generation,Identity(),"PAPlayer",true,[] {
      errno=0;auto token=pthread_override_qos_class_start_np(pthread_self(),QOS_CLASS_USER_INITIATED,0);
      return owned_override::StartResult{token,token ? 0 : (errno ? errno : -1)};
    },2));
    CHECK(pthread_get_qos_class_np(pthread_self(),&during,&duringRelative)==0);
    {std::unique_lock<std::mutex> lock(mutex);acquired=true;cv.notify_all();cv.wait(lock,[&]{return done;});}
    CHECK(pthread_get_qos_class_np(pthread_self(),&after,&afterRelative)==0);
  });
  {std::unique_lock<std::mutex> lock(mutex);cv.wait(lock,[&]{return acquired;});}
  CHECK(state.Get().active && state.Get().targetId!=owner);
  state.Control(owner,3,false,end,3);CHECK(state.Get().outstanding==0 && state.Get().lastEndStatus==0);
  {std::lock_guard<std::mutex> lock(mutex);done=true;cv.notify_all();}
  worker.join();state.Finish(owner,end,4);
  CHECK(before==during && during==after && beforeRelative==duringRelative && duringRelative==afterRelative);
  CHECK(state.Get().starts==1 && state.Get().endAttempts==1 && state.Get().ends==1);
  std::cout<<"macOS own-thread override passed: requested_before="<<before<<" during="<<during<<" after="<<after
           <<" starts=1 ends=1 outstanding=0 end_status=0\n";
}
