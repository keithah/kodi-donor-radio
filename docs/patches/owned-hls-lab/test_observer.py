"""Evidence survives unavailable Kodi and fixture teardown endpoints."""
import argparse
import types
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('hls_observer', Path(__file__).with_name('observe-hls.py'))
observer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(observer)

class ObservationPersistenceTests(unittest.TestCase):
    def test_teardown_failures_preserve_completed_timeline(self):
        with tempfile.TemporaryDirectory() as directory:
            capture = Path(directory)
            report = {'timeline': [{'playing': False}], 'actions': [{'action': 'pause'}]}
            def failed_rpc(*args, **kwargs):
                saved = json.loads((capture / 'kodi-clock-observation.json').read_text())
                self.assertEqual(saved, report)
                raise RuntimeError('Kodi gone')
            with patch.object(observer, 'rpc', side_effect=failed_rpc), patch.object(observer, 'urlopen', side_effect=OSError('fixture gone')):
                observer.save_observation(capture, report, 19092)
            saved = json.loads((capture / 'kodi-clock-observation.json').read_text())
            self.assertEqual(saved['timeline'], [{'playing': False}])
            self.assertEqual(saved['actions'], [{'action': 'pause'}])
            self.assertEqual(saved['stop_error'], 'RuntimeError')
            self.assertEqual(saved['fixture'], {'error': 'OSError'})

class ObservationSchedulingTests(unittest.TestCase):
    def test_invalid_poll_intervals(self):
        for value in ('0','-.1','0.099','5.01','nan','inf'):
            with self.subTest(value=value),self.assertRaises(argparse.ArgumentTypeError):
                observer.poll_interval(value)
        self.assertEqual(observer.poll_interval('1'),1.0)
    def test_one_hz_observations_and_wall_due_pause(self):
        now=[0.0]; calls=[]; sleeps=[]
        def sleep(seconds):
            sleeps.append(seconds); now[0]+=seconds
        def rpc(method,params=None,**kwargs):
            calls.append((now[0],method,params))
            if method=='Player.GetActivePlayers': return [{'playerid':0,'type':'audio'}]
            if method=='Player.GetItem':return {'item':{'file':'fixture','dynpath':'fixture'}}
            if method=='Player.GetProperties':
                return dict(time={'hours':0,'minutes':0,'seconds':0,'milliseconds':0},
                            totaltime={'hours':0,'minutes':3,'seconds':0,'milliseconds':0},
                            speed=1,live=True,canseek=False)
            return 'OK'
        args=types.SimpleNamespace(action='pause',seconds=9.2,port=19093,poll_interval=1.0)
        report=dict(timeline=[],actions=[])
        with patch.object(observer,'rpc',side_effect=rpc),patch.object(observer.time,'monotonic',side_effect=lambda:now[0]),patch.object(observer.time,'sleep',side_effect=sleep),patch.object(observer,'host_ns',side_effect=lambda:int(now[0]*1e9)):
            observer.observe(report,args,'fixture')
        action_status=[t for t,m,p in calls if m=='Player.GetProperties' and p['properties']==['time','totaltime','speed']]
        self.assertEqual(len(action_status),2)
        self.assertTrue(8<=action_status[0]<=8.100001)
        self.assertTrue(9<=action_status[1]<=9.200001)
        observation_times=[t for t,m,p in calls if m=='Player.GetProperties' and 'live' in p['properties']]
        self.assertGreaterEqual(len(observation_times),9)
        self.assertLessEqual(len(observation_times),10)
        self.assertTrue(all(b-a>=.999999 for a,b in zip(observation_times,observation_times[1:])))
        self.assertTrue(all(0<delay<=.1 for delay in sleeps))
        self.assertEqual(report['pause_schedule'],[(8,1),(45,3),(90,20),(160,3)])
        now[0]=0.0; calls.clear(); args.seconds=164; report=dict(timeline=[],actions=[])
        with patch.object(observer,'rpc',side_effect=rpc),patch.object(observer.time,'monotonic',side_effect=lambda:now[0]),patch.object(observer.time,'sleep',side_effect=sleep),patch.object(observer,'host_ns',side_effect=lambda:int(now[0]*1e9)):
            observer.observe(report,args,'fixture')
        self.assertEqual([event['action'] for event in report['actions']],['pause','resume']*4)
        for event,due in zip(report['actions'][::2],(8,45,90,160)):
            elapsed=event['request_host_ns']/1e9
            self.assertTrue(due<=elapsed<=due+.100001)


class PlayerIdentityTests(unittest.TestCase):
    def test_audio_id_is_accepted_without_claiming_core(self):
        def rpc(method,params=None,**kwargs):
            if method=='Player.GetActivePlayers':return [{'playerid':0,'type':'audio','playertype':'internal'}]
            if method=='Player.GetItem':return {'item':{'file':'fixture','dynpath':'fixture'}}
            raise AssertionError(method)
        with patch.object(observer,'rpc',side_effect=rpc):
            actual=observer.expected_player('fixture',19093)
        self.assertEqual(actual,{'playerid':0,'type':'audio','playertype':'internal','file':'fixture','dynpath':'fixture'})
        self.assertNotIn('core',actual)
    def test_wrong_or_multiple_items_rejected(self):
        for players in ([{'playerid':0,'type':'audio'},{'playerid':1,'type':'video'}],[{'playerid':0,'type':'audio'}]):
            with patch.object(observer,'rpc',side_effect=lambda method,params=None,**kwargs: players if method=='Player.GetActivePlayers' else {'item':{'file':'other'}}):
                with self.assertRaises(RuntimeError):observer.expected_player('fixture',19093)

class ResolvedIdentityTests(unittest.TestCase):
    def test_resolved_dynpath_is_authoritative_over_original_plugin_metadata(self):
        def rpc(method,params=None,**kwargs):
            if method=='Player.GetActivePlayers':return [{'playerid':0,'type':'audio'}]
            return {'item':{'file':'plugin://original','dynpath':'fixture'}}
        with patch.object(observer,'rpc',side_effect=rpc):
            self.assertEqual(observer.expected_player('fixture',19093)['dynpath'],'fixture')

class VideoControlIdentityTests(unittest.TestCase):
    def test_video_mode_uses_actual_id_and_exact_dynpath(self):
        def rpc(method,params=None,**kwargs):
            if method=='Player.GetActivePlayers':return [{'playerid':7,'type':'video'}]
            self.assertEqual(params['playerid'],7)
            return {'item':{'file':'plugin://original','dynpath':'fixture'}}
        with patch.object(observer,'rpc',side_effect=rpc):
            self.assertEqual(observer.expected_player('fixture',19093,'video')['playerid'],7)
            with self.assertRaises(RuntimeError):observer.expected_player('fixture',19093,'audio')

if __name__ == '__main__':
    unittest.main()


def test_gain_trial_schedule_restore_and_exact_dispatch(tmp_path,monkeypatch):
    now=[0.0];calls=[]
    report=dict(actions=[],timeline=[])
    args=types.SimpleNamespace(output_backend='hal_callback_v1',hal_phase='controls',action='none',port=19092,player_mode='videoplayer_video_v1',seconds=50)
    def rpc(method,params=None,**kw):
        calls.append((now[0],method,params))
        if method=='Application.GetProperties':return dict(volume=37,muted=True)
        if method=='Player.GetActivePlayers':return [{'playerid':7,'type':'video'}]
        if method=='Player.GetItem':return {'item':{'dynpath':'fixture'}}
        return 'OK'
    monkeypatch.setattr(observer,'rpc',rpc)
    monkeypatch.setattr(observer,'host_ns',lambda:int(now[0]*1e9))
    monkeypatch.setattr(observer.time,'monotonic',lambda:now[0])
    monkeypatch.setattr(observer.time,'sleep',lambda s:now.__setitem__(0,now[0]+s))
    observer.gain_controls_trial(report,args,'fixture',lambda:report.update(open_response_host_ns=observer.host_ns()),tmp_path)
    dispatched=[(t,p) for t,m,p in calls if m in ('Application.SetVolume','Application.SetMute')]
    assert dispatched[:2]==[(0,{'volume':100}),(0,{'mute':False})]
    assert dispatched[-2:]==[(50,{'volume':37}),(50,{'mute':True})]
    for (actual,params),due in zip(dispatched[2:-2],observer.GAIN_CONTROLS):
        assert abs(actual-due['at'])<1e-6
        assert params=={('mute' if 'muted' in due else 'volume'):due.get('muted',due.get('volume'))}
    assert [(t,m) for t,m,p in calls if m=='Player.Stop']==[(40,'Player.Stop')]
    saved=json.loads((tmp_path/'kodi-clock-observation.json').read_text())
    assert saved['original_gain']=={'volume':37,'muted':True}
    assert saved['gain_restore']['restored'] is True


def test_gain_trial_failure_persists_and_never_retries_or_toggles(tmp_path,monkeypatch):
    now=[0.0];calls=[];report=dict(actions=[],timeline=[])
    args=types.SimpleNamespace(output_backend='hal_callback_v1',hal_phase='controls',action='none',port=19092,player_mode='videoplayer_video_v1',seconds=50)
    def rpc(method,params=None,**kw):
        calls.append((now[0],method,params))
        if method=='Application.GetProperties':return dict(volume=37,muted=True)
        if method=='Player.GetActivePlayers':return [{'playerid':7,'type':'video'}]
        if method=='Player.GetItem':return {'item':{'dynpath':'wrong'}}
        return 'OK'
    monkeypatch.setattr(observer,'rpc',rpc);monkeypatch.setattr(observer,'host_ns',lambda:int(now[0]*1e9))
    monkeypatch.setattr(observer.time,'monotonic',lambda:now[0]);monkeypatch.setattr(observer.time,'sleep',lambda s:now.__setitem__(0,now[0]+s))
    with __import__('pytest').raises(RuntimeError):
        observer.gain_controls_trial(report,args,'fixture',lambda:report.update(open_response_host_ns=observer.host_ns()),tmp_path)
    assert not any(m=='Input.ExecuteAction' for t,m,p in calls)
    assert len([1 for t,m,p in calls if m.startswith('Application.Set')])==4
    assert report['gain_restore']['restored'] is True
    assert json.loads((tmp_path/'kodi-clock-observation.json').read_text())['gain_trial_error']=='RuntimeError'


def test_gain_trial_restrictions_before_any_action(monkeypatch,tmp_path):
    base=dict(output_backend='hal_callback_v1',hal_phase='controls',action='none',port=19092,player_mode='videoplayer_video_v1',seconds=50)
    monkeypatch.setattr(observer,'rpc',lambda *a,**kw:__import__('pytest').fail('RPC before guard'))
    for change in (dict(output_backend='ae'),dict(hal_phase=None),dict(action='pause'),dict(port=19093),dict(seconds=49)):
        with __import__('pytest').raises(ValueError):observer.gain_controls_trial({},types.SimpleNamespace(**dict(base,**change)),'fixture',lambda:None,tmp_path)
