// Harness-only dyld interposer. Separate image is required: dyld deliberately
// does not interpose calls originating in the interposer's own image.
#include <cstdlib>
#include <pthread.h>
#include <unistd.h>
static thread_local bool active=false;
extern "C" __attribute__((visibility("default"))) void hal_rt_enable(bool enabled){active=enabled;}
static void* rt_malloc(size_t size){if(active)_exit(91);return malloc(size);}
static int rt_mutex_lock(pthread_mutex_t* lock){if(active)_exit(92);return pthread_mutex_lock(lock);}
static ssize_t rt_write(int fd,const void* p,size_t n){if(active)_exit(93);return write(fd,p,n);}
#define INTERPOSE(replacement,original) \
 __attribute__((used)) static const struct{const void* replace;const void* original;} interpose_##original \
 __attribute__((section("__DATA,__interpose")))={reinterpret_cast<const void*>(replacement),reinterpret_cast<const void*>(original)};
INTERPOSE(rt_malloc,malloc)
INTERPOSE(rt_mutex_lock,pthread_mutex_lock)
INTERPOSE(rt_write,write)
