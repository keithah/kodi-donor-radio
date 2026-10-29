// Standalone aggregation tests; no Kodi/addon dependency.
#ifdef NDEBUG
#undef NDEBUG
#endif
#include "OwnedStallDiagnostics.h"
#include <cassert>
#include <sstream>
#include <limits>
int main() {
  using namespace owned_stall;
  Aggregate aggregate;
  aggregate.Record(ControlRead,100,150);
  aggregate.Record(ControlRead,200,280);
  aggregate.Record(ControlRead,500,400); // reversed bracket ignored
  auto sample=aggregate.Take();
  assert(sample.available && sample.metrics[ControlRead].count==2);
  assert(sample.metrics[ControlRead].maxNs==80);
  assert(sample.metrics[ControlRead].maxStartNs==200);
  assert(sample.metrics[ControlRead].maxEndNs==280);
  assert(sample.eventCount==0);
  sample=aggregate.Take();
  assert(sample.metrics[ControlRead].count==0 && sample.revision==2);
  aggregate.Record(AddData,0,40000000); // strict >40ms
  assert(aggregate.Take().eventCount==0);
  for (unsigned i=0;i<20;++i) aggregate.Record(TelemetryFlush,i,50000000+i);
  sample=aggregate.Take();
  assert(sample.eventCount==12 && sample.droppedEvents==8);
  assert(sample.events[0].startNs==0 && sample.events[11].endNs==50000011);
  assert(sample.metrics[TelemetryFlush].count==20);
  std::ostringstream json; WriteJson(json,sample);
  assert(json.str().size()<12000); // leaves room for unchanged original clock fields
  assert(json.str().find("start_mach_ns")!=std::string::npos);
  Snapshot worst;
  worst.available=true;
  const auto maximum=std::numeric_limits<uint64_t>::max();
  worst.revision=maximum; worst.droppedContention=maximum; worst.droppedEvents=maximum;
  for (auto& metric:worst.metrics) metric=Metric{maximum,maximum,maximum,maximum};
  worst.eventCount=worst.events.size();
  for (auto& event:worst.events) event=Event{RendererOwnedWait,maximum,maximum};
  std::ostringstream large; WriteJson(large,worst);
  assert(large.str().size()<10000); // original clock record has >6KiB headroom

}
