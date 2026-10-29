#include "OwnedVideoHost.h"
#include <kodi/addon-instance/Inputstream.h>
extern "C" {
#include <libavcodec/avcodec.h>
#include <libavutil/version.h>
}
#include "KodiVideoReadiness.h"
#include <cstdlib>
#include <cstring>
#include <iostream>
#define CHECK(x) do {if(!(x)){std::cerr<<__LINE__<<": "<<#x<<"\n";std::exit(1);}}while(false)
int main() {
 std::cout<<"FFmpeg runtime version: "<<av_version_info()<<"\n"<<std::flush;
 CHECK(std::string(av_version_info())=="9.0.2-Kodi");
 kodi::addon::InputstreamInfo source,host;
 source.SetStreamType(INPUTSTREAM_TYPE_AUDIO);source.SetCodecName("aac");source.SetSampleRate(48000);source.SetChannels(2);
 source.SetExtraData(std::vector<uint8_t>{0x11,0x90});
 CHECK(owned_video::Project(source,host));
 CDVDDemuxClient gate;gate.m_videoStreamPlaying=0;
 gate.m_streams[0]=std::make_shared<ReadinessStream>();
 gate.m_streams[0]->type=StreamType::VIDEO;gate.m_streams[0]->codec=AV_CODEC_ID_RAWVIDEO;
 gate.m_streams[0]->extraData=false;CHECK(!gate.IsVideoReady()); // actual old metadata rejected
 gate.m_streams[0]->extraData=host.GetExtraDataSize()!=0;
 CHECK(gate.IsVideoReady()); // exact external core method, not an addon readiness rule
 CHECK(host.GetExtraData()==std::vector<uint8_t>{0});
 CHECK(host.GetCStructure()->m_ExtraData==host.GetExtraData().data() && host.GetCStructure()->m_ExtraData[0]==0);
 CHECK(source.GetExtraData()==(std::vector<uint8_t>{0x11,0x90}));
 // SDK setter must own bytes after the temporary input has been destroyed.
 kodi::addon::InputstreamInfo lifetime;
 {std::vector<uint8_t> temporary{0};lifetime.SetExtraData(temporary);temporary[0]=0xff;}
 CHECK(lifetime.GetExtraData()==std::vector<uint8_t>{0});
 const auto* wire=lifetime.GetCStructure();
 CHECK(wire->m_ExtraSize==1 && wire->m_ExtraData==lifetime.GetExtraData().data() && wire->m_ExtraData[0]==0);
 const AVCodec* codec=avcodec_find_decoder_by_name("rawvideo");CHECK(codec);
 AVCodecContext* context=avcodec_alloc_context3(codec);CHECK(context);
 context->width=host.GetWidth();context->height=host.GetHeight();context->codec_tag=host.GetCodecFourCC();
 context->pkt_timebase={1,1000000};context->extradata_size=host.GetExtraDataSize();
 context->extradata=static_cast<uint8_t*>(av_mallocz(context->extradata_size+AV_INPUT_BUFFER_PADDING_SIZE));CHECK(context->extradata);
 std::memcpy(context->extradata,host.GetExtraData().data(),context->extradata_size);
 CHECK(avcodec_open2(context,codec,nullptr)==0);
 AVPacket* packet=av_packet_alloc();AVFrame* frame=av_frame_alloc();CHECK(packet && frame);
 CHECK(av_new_packet(packet,owned_video::Bytes)==0);
 owned_video::Timeline timeline;CHECK(timeline.Open(10));CHECK(timeline.Arm());
 for(int n=0;n<2;++n) {
  double pts,duration;CHECK(timeline.Next(11+n,0,false)==owned_video::Result::Frame);
  CHECK(timeline.Fill(packet->data,packet->size,11+n,pts,duration));
  packet->pts=packet->dts=static_cast<int64_t>(pts);packet->duration=static_cast<int64_t>(duration);
  CHECK(avcodec_send_packet(context,packet)==0);CHECK(avcodec_receive_frame(context,frame)==0);
  CHECK(frame->width==16 && frame->height==16 && frame->format==AV_PIX_FMT_YUV420P);
  CHECK(frame->pts==n*100000 && frame->best_effort_timestamp==n*100000);
  CHECK((frame->flags & AV_FRAME_FLAG_KEY) && frame->pict_type==AV_PICTURE_TYPE_I);
  for(int plane=0;plane<3;++plane)for(int y=0;y<(plane?8:16);++y)for(int x=0;x<(plane?8:16);++x)
    CHECK(frame->data[plane][y*frame->linesize[plane]+x]==(plane?128:16));
  av_frame_unref(frame);
 }
 av_frame_free(&frame);av_packet_free(&packet);avcodec_free_context(&context);
 std::cout<<"exact Kodi readiness + SDK-owned marker + FFmpeg9.0.2 YUV420P/planes/PTS/KEY/I passed\n";
}
