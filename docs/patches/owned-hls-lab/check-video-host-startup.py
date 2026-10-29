#!/usr/bin/env python3
"""Compile exact external Kodi readiness bodies and decode with installed FFmpeg9.0.2."""
import argparse
import hashlib
import os
from pathlib import Path
import shlex
import subprocess

p=argparse.ArgumentParser()
for name in ('native-src','kodi-src','ffmpeg-prefix','output-dir'):p.add_argument('--'+name,type=Path,required=True)
a=p.parse_args();a.output_dir.mkdir(parents=True,exist_ok=True)
core=a.kodi_src/'xbmc/cores/VideoPlayer/DVDDemuxers/DVDDemuxClient.cpp'
s=core.read_text()
methods=[]
for name in ('IsVideoReady','CodecHasExtraData'):
 start=s.index('bool CDVDDemuxClient::'+name+'(');end=s.index('\n}',start)+2
 methods.append(s[start:end])
header='''#include <map>
#include <memory>
enum class StreamType { AUDIO,VIDEO };
struct ReadinessStream { StreamType type; AVCodecID codec; bool extraData; };
class CDVDDemuxClient {
public:
 int m_videoStreamPlaying=-1;
 std::map<int,std::shared_ptr<ReadinessStream>> m_streams;
 bool IsVideoReady(); bool CodecHasExtraData(AVCodecID id);
};
'''+ '\n'.join(methods)+'\n'
(a.output_dir/'KodiVideoReadiness.h').write_text(header)
print('External Kodi readiness SHA256:',hashlib.sha256('\n'.join(methods).encode()).hexdigest(),flush=True)
flags=[]
for library in ('libavcodec','libswresample','libavutil'):
 for line in (a.ffmpeg_prefix/'lib/pkgconfig'/(library+'.pc')).read_text().splitlines():
  if line.startswith(('Libs:','Libs.private:')):
   value=line.split(':',1)[1].replace('${libdir}',str(a.ffmpeg_prefix/'lib'))
   flags+=shlex.split(value)
for mode in ('debug','ndebug'):
 exe=a.output_dir/('video-host-startup-'+mode)
 command=['clang++','-std=c++14','-Wall','-Wextra','-Werror','-pthread']+(['-DNDEBUG'] if mode=='ndebug' else [])
 command+=['-I'+str(a.native_src),'-I'+str(a.output_dir),'-isystem',str(a.kodi_src/'xbmc/addons/kodi-dev-kit/include'),'-isystem',str(a.ffmpeg_prefix/'include'),str(Path(__file__).with_name('test_video_host_decode.cpp')),'-o',str(exe)]+flags
 subprocess.run(command,check=True);subprocess.run([str(exe)],check=True)
