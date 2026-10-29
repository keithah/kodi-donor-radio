import importlib.util
import json
from pathlib import Path


spec = importlib.util.spec_from_file_location('owned_control_context', Path(__file__).with_name('control_context.py'))
context = importlib.util.module_from_spec(spec)
spec.loader.exec_module(context)


def test_missing_player_callbacks_are_reported_without_shifting_counts(tmp_path):
    (tmp_path/'kodi-clock-observation.json').write_text(json.dumps({'actions': [
        {'action': 'pause', 'cycle': 1, 'request_host_ns': 100, 'response_host_ns': 200},
        {'action': 'pause', 'cycle': 2, 'request_host_ns': 300, 'response_host_ns': 400},
    ]}))
    (tmp_path/'capture.json').write_text(json.dumps({'first_host_pts': {'value': 0}}))
    callbacks=tmp_path/'callbacks.jsonl'
    callbacks.write_text(json.dumps({'event':'pause','monotonic_ns':250})+'\n')
    report={'internal_quiet_gaps': []}

    context.add_control_context(report, tmp_path, callbacks)

    details=report['control_context']
    assert details['callback_count_mismatch']=={'pause': {'actions': 2, 'callbacks': 1}}
    assert len(details['callback_latency'])==1
