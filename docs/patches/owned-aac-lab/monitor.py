# Isolated loopback experiment only. Controls own renderer via normal Kodi callbacks.
import json, os, time
from pathlib import Path
import xbmc
ROOT=Path('/private/tmp/kodi-audio-validation-20261027/native-pause-repair/owned-renderer')
def state(value):
 tmp=ROOT/'control.tmp'
 tmp.write_text(value)
 os.replace(str(tmp),str(ROOT/'control'))
 with (ROOT/'callbacks.jsonl').open('a') as f:
  f.write(json.dumps({'event':value,'monotonic_ns':time.monotonic_ns()})+'\n')
class Player(xbmc.Player):
 def onAVStarted(self): state('play')
 def onPlayBackPaused(self): state('pause')
 def onPlayBackResumed(self): state('resume')
 def onPlayBackStopped(self): state('stop')
 def onPlayBackEnded(self): state('stop')
p=Player()
m=xbmc.Monitor()
state('waiting')
(ROOT/'monitor-ready').write_text(str(time.monotonic_ns()))
for _ in range(1200):
 if m.waitForAbort(.1) or (ROOT/'monitor-stop').exists(): break
(ROOT/'monitor-ready').unlink(missing_ok=True)
