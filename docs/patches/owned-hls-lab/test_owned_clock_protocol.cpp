// Standalone C++14 behavior checks. CHECK remains active under -DNDEBUG.
#include "OwnedClockProtocol.h"
#include "OwnedClockTransport.h"
#include <sys/stat.h>
#include <unistd.h>
#include <fstream>
#include <cstdlib>
#include <iostream>
using namespace owned_clock;
#define CHECK(x) do { if (!(x)) { std::cerr << __LINE__ << ": " #x "\n"; std::exit(1); } } while (0)
const std::string nonce(32, 'a'), incarnation(32, 'b');
std::string Wire(unsigned revision, const std::string& state, uint64_t issued = 10) {
  return "v1 " + nonce + " " + incarnation + " " + std::to_string(revision) + " " + state + " " + std::to_string(issued) + "\n";
}
int main() {
  Command c;
  CHECK(Parse(Wire(1, "waiting"), c));
  CHECK(c.revision == 1 && c.issued == 10 && c.state == "waiting");
  for (const std::string& bad : {Wire(0,"play"), Wire(65,"play"), Wire(2,"seek"),
       Wire(2,"play") + "x", Wire(2,"play",0), std::string(513,'a'),
       std::string("v1 ") + std::string(32,'A') + " " + incarnation + " 1 waiting 10\n",
       "v1 " + nonce + " " + incarnation + " 01 waiting 10\n",
       "v1 " + nonce + " " + incarnation + " 1 waiting 18446744073709551616\n"}) CHECK(!Parse(bad,c));
  Authority launch(nonce,incarnation,30);
  CHECK(!launch.Apply(Wire(1,"waiting",40),50));
  Authority oldCallback(nonce,incarnation,30);
  CHECK(oldCallback.Apply(Wire(1,"waiting",10),40));
  CHECK(!oldCallback.Apply(Wire(2,"play",25),50));
  Authority a(nonce, incarnation);
  CHECK(!a.Eligible(a.Generation()));
  CHECK(a.Apply(Wire(1,"waiting"),20));
  const auto waitingGeneration = a.Generation();
  CHECK(a.Apply(Wire(1,"waiting"),21));
  CHECK(a.Generation() == waitingGeneration); // identical reads are idempotent
  a.ConfirmSource(true);
  CHECK(a.Apply(Wire(2,"play",22),23));
  CHECK(a.Eligible(a.Generation()));
  CHECK(!a.Eligible(waitingGeneration)); // a changed generation cannot qualify a bracket
  CHECK(a.Apply(Wire(3,"pause",24),25));
  CHECK(!a.Eligible(a.Generation()));
  CHECK(a.Apply(Wire(4,"resume",26),27));
  CHECK(a.Eligible(a.Generation()));
  a.Speed(0);
  CHECK(!a.Eligible(a.Generation()));
  a.Speed(1000);
  CHECK(!a.Eligible(a.Generation())); // normal speed alone cannot recover a pause tail
  CHECK(a.Apply(Wire(5,"resume",28),29));
  CHECK(a.Eligible(a.Generation()));
  CHECK(!a.Apply(Wire(5,"pause",28),30)); // conflicting duplicate permanently retires
  CHECK(a.Retired());
  CHECK(a.Apply(Wire(6,"pause",31),32)); // valid audio controls still apply after retirement
  CHECK(a.Apply(Wire(7,"resume",33),34));
  CHECK(!a.Eligible(a.Generation()));
  for (const std::string& bad : {Wire(2,"play"), Wire(1,"play"), Wire(1,"waiting",40)}) {
    Authority b(nonce,incarnation); CHECK(!b.Apply(bad,30)); CHECK(b.Retired());
  }
  Authority b(nonce,incarnation);
  CHECK(b.Apply(Wire(1,"waiting"),20));
  CHECK(b.Apply(Wire(2,"play",21),22));
  b.ConfirmSource(false);
  CHECK(b.Retired());
  b.ConfirmSource(true);
  CHECK(!b.Eligible(b.Generation()));
  Authority d(nonce,incarnation);
  CHECK(d.Apply(Wire(1,"waiting"),20));
  CHECK(d.Apply(Wire(2,"stop",21),22));
  CHECK(d.Retired());
  CHECK(!d.Apply(Wire(3,"play",23),24));
  Authority e(nonce,incarnation);
  CHECK(e.Apply(Wire(1,"waiting"),20));
  CHECK(e.Apply(Wire(2,"play",21),22));
  e.Speed(2000); CHECK(e.Retired());
  Authority order(nonce,incarnation);
  CHECK(order.Apply(Wire(1,"waiting"),20));
  order.ConfirmSource(true);
  CHECK(order.Apply(Wire(2,"play",21),22));
  order.Speed(0);
  CHECK(order.Apply(Wire(3,"resume",23),24));
  CHECK(!order.Eligible(order.Generation()));
  order.Speed(1000);
  CHECK(order.Eligible(order.Generation())); // both facts may arrive in either callback order
  Authority f(nonce,incarnation);
  CHECK(f.Apply(Wire(1,"waiting"),20));
  for (unsigned i=2;i<=64;++i) CHECK(f.Apply(Wire(i,i==2?"play":i%2?"pause":"resume",20+i),100));
  CHECK(!f.Apply(Wire(65,"pause",85),100));
  char temp[] = "/private/tmp/owned-clock-test-XXXXXX";
  const char* directory = mkdtemp(temp); CHECK(directory);
  {
    Transport t; CHECK(t.Open(directory,"{\"protocol\":1}\n"));
    CHECK(!t.Open(directory,"{}\n")); // never overwrite exclusive identity files
  }
  const std::string occupied=std::string(directory)+"/occupied";
  CHECK(mkdir(occupied.c_str(),0700)==0);
  std::ofstream(occupied+"/native.jsonl")<<"retained";
  { Transport t;CHECK(!t.Open(occupied,"{}\n")); }
  CHECK(access((occupied+"/hello.json").c_str(),F_OK)!=0); // failed ledger setup must not publish hello
  const std::string commandsDir = std::string(directory)+"/commands";
  CHECK(mkdir(commandsDir.c_str(),0700)==0);
  {
    Transport t; CHECK(t.Open(commandsDir,"{}\n"));
    CHECK(t.Append("{\"kind\":\"test\"}"));
    std::ofstream(commandsDir+"/command-000001.txt") << Wire(1,"waiting");
    CHECK(chmod((commandsDir+"/command-000001.txt").c_str(),0600)==0);
    std::vector<std::string> pending;
    CHECK(t.Poll(0,pending) && pending.size()==1);
    CHECK(t.Poll(1,pending) && pending.empty());
    std::ofstream(commandsDir+"/command-000001.txt") << Wire(1,"play");
    CHECK(!t.Poll(1,pending)); // rewriting an acknowledged command retires transport
  }
  const std::string gapDir=std::string(directory)+"/gap";
  CHECK(mkdir(gapDir.c_str(),0700)==0);
  {
    Transport t; CHECK(t.Open(gapDir,"{}\n"));
    std::ofstream(gapDir+"/command-000002.txt") << Wire(2,"play");
    CHECK(chmod((gapDir+"/command-000002.txt").c_str(),0600)==0);
    std::vector<std::string> pending; CHECK(!t.Poll(0,pending));
  }
  const std::string link=std::string(directory)+"/link";
  CHECK(symlink(commandsDir.c_str(),link.c_str())==0);
  Transport symlinked; CHECK(!symlinked.Open(link,"{}\n"));
  const std::string insecure=std::string(directory)+"/insecure";
  CHECK(mkdir(insecure.c_str(),0755)==0);
  Transport publicDir; CHECK(!publicDir.Open(insecure,"{}\n"));
  const std::string helloDir=std::string(directory)+"/hello-change";
  CHECK(mkdir(helloDir.c_str(),0700)==0);
  {
    Transport t;CHECK(t.Open(helloDir,"{}\n"));
    std::ofstream(helloDir+"/hello.json")<<"{changed}\n";
    std::vector<std::string> pending;CHECK(!t.Poll(0,pending));
  }
  const std::string boundedDir=std::string(directory)+"/bounded";
  CHECK(mkdir(boundedDir.c_str(),0700)==0);
  {
    Transport t; CHECK(t.Open(boundedDir,"{}\n"));
    for(unsigned i=1;i<=10;++i) {
      char name[32];std::snprintf(name,sizeof(name),"/command-%06u.txt",i);
      std::ofstream(boundedDir+name)<<Wire(i,i==1?"waiting":i==2?"play":i%2?"pause":"resume");
      CHECK(chmod((boundedDir+name).c_str(),0600)==0);
    }
    std::vector<std::string> pending;
    CHECK(t.Poll(0,pending) && pending.size()==8);
    CHECK(t.Poll(8,pending) && pending.size()==2);
    CHECK(unlink((boundedDir+"/command-000001.txt").c_str())==0);
    CHECK(!t.Poll(10,pending));
  }
  const std::string overDir=std::string(directory)+"/oversized";
  CHECK(mkdir(overDir.c_str(),0700)==0);
  {
    Transport t;CHECK(t.Open(overDir,"{}\n"));
    std::ofstream(overDir+"/command-000001.txt")<<std::string(513,'x');
    CHECK(chmod((overDir+"/command-000001.txt").c_str(),0600)==0);
    std::vector<std::string> pending; CHECK(!t.Poll(0,pending));
  }
  const std::string ledgerDir=std::string(directory)+"/ledger";
  CHECK(mkdir(ledgerDir.c_str(),0700)==0);
  {
    Transport t;CHECK(t.Open(ledgerDir,"{}\n"));
    CHECK(t.Append("{}"));
    CHECK(truncate((ledgerDir+"/native.jsonl").c_str(),0)==0);
    CHECK(!t.Append("{}"));
  }
  const std::string replacedDir=std::string(directory)+"/replace";
  CHECK(mkdir(replacedDir.c_str(),0700)==0);
  {
    Transport t;CHECK(t.Open(replacedDir,"{}\n"));
    CHECK(rename(replacedDir.c_str(),(replacedDir+"-old").c_str())==0);
    CHECK(mkdir(replacedDir.c_str(),0700)==0);
    std::vector<std::string> pending; CHECK(!t.Poll(0,pending));
  }
  const std::string symlinkCommand=std::string(directory)+"/symlink-command";
  CHECK(mkdir(symlinkCommand.c_str(),0700)==0);
  {
    Transport t;CHECK(t.Open(symlinkCommand,"{}\n"));
    CHECK(symlink((commandsDir+"/command-000001.txt").c_str(),(symlinkCommand+"/command-000001.txt").c_str())==0);
    std::vector<std::string> pending; CHECK(!t.Poll(0,pending));
  }
  // Damaging acknowledged evidence retires the clock but must not suppress later
  // individually valid exact-next audio controls from the same pinned session.
  for (const std::string& damage : {"old-command", "old-missing", "hello"}) {
    const std::string controls=std::string(directory)+"/retired-"+damage;
    CHECK(mkdir(controls.c_str(),0700)==0);
    Transport transport; CHECK(transport.Open(controls,"{}\n"));
    Authority owner(nonce,incarnation);
    auto publish=[&](unsigned revision,const std::string& state) {
      char name[32];std::snprintf(name,sizeof(name),"/command-%06u.txt",revision);
      std::ofstream(controls+name)<<Wire(revision,state,10+revision);
      CHECK(chmod((controls+name).c_str(),0600)==0);
    };
    auto apply=[&](bool evidenceExpected,unsigned revision,const std::string& state) {
      std::vector<std::string> pending;
      const bool intact=transport.Poll(owner.Control().revision,pending);
      CHECK(intact==evidenceExpected);
      if (!intact) owner.Revoke("command_transport");
      CHECK(pending.size()==1);
      CHECK(owner.Apply(pending.front(),100));
      CHECK(owner.Control().revision==revision && owner.Control().state==state);
    };
    publish(1,"waiting"); apply(true,1,"waiting");
    publish(2,"play"); apply(true,2,"play");
    owner.ConfirmSource(true); CHECK(owner.Eligible(owner.Generation()));
    if(damage=="old-command") std::ofstream(controls+"/command-000001.txt")<<"damaged\n";
    else if(damage=="old-missing") CHECK(unlink((controls+"/command-000001.txt").c_str())==0);
    else std::ofstream(controls+"/hello.json")<<"damaged\n";
    publish(3,"pause"); apply(false,3,"pause"); CHECK(owner.Retired());
    publish(4,"resume"); apply(false,4,"resume"); CHECK(!owner.Eligible(owner.Generation()));
    publish(5,"stop"); apply(false,5,"stop"); CHECK(owner.Retired());
  }
  // A later malformed record must not discard a safe contiguous prefix. It also
  // must never be skipped, or become an accepted control by rewriting its file.
  const std::string prefixDir=std::string(directory)+"/prefix";
  CHECK(mkdir(prefixDir.c_str(),0700)==0);
  {
    Transport t; CHECK(t.Open(prefixDir,"{}\n"));
    std::ofstream(prefixDir+"/command-000001.txt")<<Wire(1,"waiting",11);
    std::ofstream(prefixDir+"/command-000002.txt")<<Wire(2,"play",12);
    std::ofstream(prefixDir+"/command-000003.txt")<<"malformed\n";
    for(unsigned i=1;i<=3;++i) {
      char name[32];std::snprintf(name,sizeof(name),"/command-%06u.txt",i);
      CHECK(chmod((prefixDir+name).c_str(),0600)==0);
    }
    std::vector<std::string> pending;
    CHECK(!t.Poll(0,pending)); CHECK(pending.size()==2);
    Authority owner(nonce,incarnation); owner.Revoke("command_transport");
    for(const auto& wire:pending) CHECK(owner.Apply(wire,100));
    CHECK(owner.Control().state=="play" && owner.Control().revision==2 && owner.Retired());
    CHECK(!t.Poll(2,pending)); CHECK(pending.empty());
    std::ofstream(prefixDir+"/command-000003.txt")<<Wire(3,"pause",13);
    CHECK(!t.Poll(2,pending)); CHECK(pending.empty());
  }
  // Keep test directories for inspection, like the diagnostic's retained evidence.
  std::cout << "owned clock protocol checks passed (NDEBUG-safe)\n";
}
