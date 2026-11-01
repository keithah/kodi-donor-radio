// Standalone aggregation tests; no Kodi/addon dependency.
#include "OwnedStallDiagnostics.h"
#include <cstdlib>
#include <iostream>
#define CHECK(x) do { if (!(x)) { std::cerr << __LINE__ << ": " << #x << "\n"; std::exit(1); } } while (false)
#include <sstream>
#include <limits>
#include <fstream>
uint64_t fakeNow=0;
uint64_t clockNow() { return fakeNow; }
int main(int argc, char** argv) {
  using namespace owned_stall;
  Aggregate aggregate;
  aggregate.Record(ControlRead,100,150);
  aggregate.Record(ControlRead,200,280);
  aggregate.Record(ControlRead,500,400); // reversed bracket ignored
  auto sample=aggregate.Take();
  CHECK(sample.available && sample.metrics[ControlRead].count==2);
  CHECK(sample.metrics[ControlRead].maxNs==80);
  CHECK(sample.metrics[ControlRead].maxStartNs==200);
  CHECK(sample.metrics[ControlRead].maxEndNs==280);
  CHECK(sample.eventCount==0);
  sample=aggregate.Take();
  CHECK(sample.metrics[ControlRead].count==0 && sample.revision==2);
  aggregate.Record(AddData,0,40000000); // strict >40ms
  CHECK(aggregate.Take().eventCount==0);
  for (unsigned i=0;i<20;++i) aggregate.Record(TelemetryFlush,i,50000000+i);
  sample=aggregate.Take();
  CHECK(sample.eventCount==12 && sample.droppedEvents==8);
  CHECK(sample.events[0].startNs==0 && sample.events[11].endNs==50000011);
  CHECK(sample.metrics[TelemetryFlush].count==20);
  std::ostringstream json; WriteJson(json,sample);
  CHECK(json.str().size()<12000); // leaves room for unchanged original clock fields
  CHECK(json.str().find("\"version\":3,\"header\":[40000000,true,4,0,8,0]")!=std::string::npos);
  CHECK(json.str().find("[3,20,50000000,19,50000019]")!=std::string::npos);
  CHECK(json.str().find("\"events\":[[3,0,50000000],[3,1,50000001]")!=std::string::npos);
  // New component timings must survive serialization with exact completed brackets.
  const char* added[] = {"output_guard_before", "output_guard_after", "clock_observation",
                        "native_append", "host_demux_call", "host_demux_interarrival"};
  for (const auto* name:added) {
    size_t index=0;
    while(index<Count && std::string(Names[index])!=name) ++index;
    CHECK(index<Count);
    aggregate.Record(static_cast<Operation>(index),100,50000101);
  }
  auto components=aggregate.Take();
  CHECK(components.eventCount==6);
  std::ostringstream componentJson; WriteJson(componentJson,components);
  CHECK(componentJson.str().find("\"version\":3")!=std::string::npos);
  std::string expected="{\"version\":3,\"header\":[40000000,true,5,0,0,0],\"operations\":[";
  for(size_t i=0;i<22;++i) {
    if(i)expected+=',';
    expected+='['+std::to_string(i)+(i<16 ? ",0,0,0,0]" : ",1,50000001,100,50000101]");
  }
  expected+="],\"events\":[[16,100,50000101],[17,100,50000101],[18,100,50000101],[19,100,50000101],[20,100,50000101],[21,100,50000101]],\"threads\":[]}";
  CHECK(componentJson.str()==expected);
  // RAII includes early exits; interarrival is separate from service time.
  auto service=[&]() {
    Span span(aggregate,HostDemuxCall,clockNow,true);
    CHECK(span.StartNs()==100000000);
    fakeNow=101000000;
    return;
  };
  fakeNow=100000000; service();
  aggregate.Record(HostDemuxInterarrival,30000000,100000000);
  auto calls=aggregate.Take();
  CHECK(calls.metrics[HostDemuxCall].maxNs==1000000);
  CHECK(calls.metrics[HostDemuxInterarrival].maxNs==70000000);
  { Span disabled(aggregate,HostDemuxCall,clockNow,false); }
  CHECK(aggregate.Take().metrics[HostDemuxCall].count==0);
  // First readback per identity only, persistent across snapshots, fixed four slots.
  unsigned reads=0;
  auto read=[&]() { ++reads; HostThread t; t.observedNs=123; t.qosStatus=0;
    t.qosClass=9; t.relativePriority=-2; return t; };
  aggregate.ObserveHostThread(42,0,read);
  aggregate.ObserveHostThread(42,0,read);
  auto threads=aggregate.Take();
  CHECK(reads==1 && threads.hostThreadCount==1);
  CHECK(threads.hostThreads[0].threadId==42 && threads.hostThreads[0].observedNs==123);
  CHECK(threads.hostThreads[0].qosClass==9 && threads.hostThreads[0].relativePriority==-2);
  aggregate.ObserveHostThread(42,0,read);
  for (uint64_t id=43;id<47;++id) aggregate.ObserveHostThread(id,0,read);
  threads=aggregate.Take();
  CHECK(reads==4 && threads.hostThreadCount==4 && threads.droppedHostThreads==1);
  CHECK(aggregate.Take().hostThreadCount==4);
  std::ostringstream threadJson; WriteJson(threadJson,threads);
  CHECK(threadJson.str().find("\"threads\":[[42,123,0,0,9,-2],[43,123,0,0,9,-2],[44,123,0,0,9,-2],[45,123,0,0,9,-2]]}")!=std::string::npos);
  if(argc==2) {
    std::ofstream output(argv[1]);
    output << "{\"kind\":\"clock_sample\",\"stall_diagnostics\":";
    WriteJson(output,components); output << "}\n";
    output << "{\"kind\":\"clock_sample\",\"stall_diagnostics\":";
    WriteJson(output,threads); output << "}\n";
    CHECK(output.good());
  }
  aggregate.Reset();
  auto fresh=aggregate.Take();
  CHECK(fresh.revision==1 && fresh.hostThreadCount==0 && fresh.droppedHostThreads==0);
  aggregate.ObserveHostThread(0,-1,[] { return HostThread{}; });
  auto failedReadback=aggregate.Take();
  CHECK(failedReadback.hostThreadCount==1 && failedReadback.hostThreads[0].qosStatus==-1);
  CHECK(failedReadback.hostThreads[0].threadId==0 && failedReadback.hostThreads[0].identityStatus==-1);
  Snapshot worst;
  worst.available=true;
  const auto maximum=std::numeric_limits<uint64_t>::max();
  worst.revision=maximum; worst.droppedContention=maximum; worst.droppedEvents=maximum;
  for (auto& metric:worst.metrics) metric=Metric{maximum,maximum,0,maximum};
  worst.hostThreadCount=worst.hostThreads.size();
  worst.droppedHostThreads=maximum;
  for (size_t i=0;i<worst.hostThreads.size();++i) worst.hostThreads[i]=HostThread{maximum-i,maximum,2147483647,2147483647,2147483647,-2147483647};
  worst.eventCount=worst.events.size();
  for (auto& event:worst.events) event=Event{RendererOwnedWait,0,maximum};
  std::ostringstream large; WriteJson(large,worst);
  CHECK(large.str().find("[0,18446744073709551615,18446744073709551615,0,18446744073709551615]")!=std::string::npos);
  CHECK(large.str().size()<10000); // original clock record has >6KiB headroom
  if(argc==2) {
    std::ofstream output(argv[1],std::ios::app);
    output << "{\"kind\":\"clock_sample\",\"stall_diagnostics\":" << large.str() << "}\n";
    CHECK(output.good());
  }

}
