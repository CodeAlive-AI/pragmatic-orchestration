"""Real loopback HTTP / real Porch supervisor, fake agent and auth; no AWS."""
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS / 'lib'))
from agentcore.server import Guest, handler
from agentcore.channel import RunnerChannel, UnknownRunnerOutcome


class GuestTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config = self.root/'fleet.json'
        self.config.write_text(json.dumps(dict(version=1,state_path=str(self.root/'state/fleet.db'),hosts=[dict(
            id='guest',transport='local',porch=str(SCRIPTS/'porch'),capacity=1,
            request_timeout_seconds=20,agents=['codex'],workspaces={'task':str(self.root)})])))
        self.env = patch.dict(os.environ, dict(PORCH_CONFIG=str(SCRIPTS/'tests/fixtures/test-config.json'),
            PORCH_BIN_CODEX=str(SCRIPTS/'tests/fakes/steer/fake-codex-steer'),
            PORCH_STEER_DIR=str(self.root/'steer'),PORCH_RUN_DIR=str(self.root/'artifacts'),
            PORCH_OUTPUT_DIR=str(self.root/'artifacts'),PORCH_FAKE_STEER_SLOW='0.1'))
        self.env.start();self.addCleanup(self.env.stop)
        self.guest = Guest(self.config)
        self.session = 'porch-00000000-0000-0000-0000-000000000001'
        self.auth = patch.object(self.guest,'subscription_ready',return_value=True)
        self.auth_mock = self.auth.start();self.addCleanup(self.auth.stop)
        self.server = ThreadingHTTPServer(('127.0.0.1',0),handler(self.guest))
        thread=threading.Thread(target=self.server.serve_forever,daemon=True);thread.start()
        def cleanup():
            self.server.shutdown();self.server.server_close();thread.join()
        self.addCleanup(cleanup)
        self.url='http://127.0.0.1:'+str(self.server.server_port)

    def invoke(self, operation, body=None, **extra):
        envelope=dict(protocol=1,session_id=self.session,boot_id=self.guest.boot_id,operation=operation,body=body,**extra)
        request=urllib.request.Request(self.url+'/invocations',data=json.dumps(envelope).encode(),headers={'Content-Type':'application/json'})
        with urllib.request.urlopen(request,timeout=25) as response:return json.load(response)

    def test_http_launch_replay_reconnect_and_collect(self):
        ready=self.invoke('ready')
        self.assertTrue(ready['runner_ready'])
        body=dict(request_id='task-1',agent='codex',workspace='task',prompt='harmless fake task')
        first=self.invoke('launch',body)
        request=json.loads(first['stdout'])['request']
        replay=self.invoke('launch',body)
        self.assertTrue(json.loads(replay['stdout'])['replayed'])
        self.assertEqual(json.loads(replay['stdout'])['request']['run_id'],request['run_id'])
        final=self.invoke('control',dict(request_id='task-1',command='wait',options=['--timeout','10','--json']))
        self.assertEqual(json.loads(final['stdout'])['status'],'completed')
        self.assertEqual(self.invoke('reconcile',dict(request_id='task-1'))['exit_code'],0)
        self.assertEqual(len(list((self.root/'steer/runs').iterdir())),1)

    def test_stale_boot_or_foreign_session_cannot_launch(self):
        self.invoke('ready')
        for envelope in [dict(protocol=1,session_id=self.session,boot_id='old',operation='launch',body={}),
                         dict(protocol=1,session_id=self.session[:-1]+'2',operation='ready')]:
            request=urllib.request.Request(self.url+'/invocations',data=json.dumps(envelope).encode())
            with self.assertRaises(urllib.error.HTTPError) as error:urllib.request.urlopen(request)
            self.assertEqual(error.exception.code,400)
            error.exception.close()
        self.assertFalse((self.root/'steer/runs').exists())

    def test_no_subscription_means_no_launch(self):
        self.auth_mock.return_value=False
        self.assertFalse(self.invoke('ready')['subscription_ready'])
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.invoke('launch',dict(request_id='task-1',agent='codex',workspace='task',prompt='fake task'))
        self.assertEqual(error.exception.code,400)
        error.exception.close()
        self.assertFalse((self.root/'steer/runs').exists())

    def test_http_health_is_not_authentication_readiness(self):
        self.auth_mock.return_value=False
        with urllib.request.urlopen(self.url+'/ping') as response:
            self.assertEqual(json.load(response)['status'],'Healthy')
        self.assertFalse(self.invoke('ready')['subscription_ready'])

    def test_channel_refuses_cross_boot_and_does_not_replay(self):
        calls=[]
        def invoke(envelope):
            calls.append(envelope)
            return self.guest.invoke(envelope)
        channel=RunnerChannel(self.session,invoke)
        channel.ready()
        original=self.guest.boot_id
        self.guest.boot_id='new-boot'
        with self.assertRaises(UnknownRunnerOutcome):channel.ready()
        self.assertEqual(channel.boot_id,original)
        self.assertEqual(len(calls),2)

    def test_channel_requires_claimed_readiness_and_identity(self):
        calls=[]
        def wrong(envelope):
            calls.append(envelope)
            return dict(protocol=1,session_id='foreign',boot_id='boot')
        channel=RunnerChannel(self.session,wrong)
        with self.assertRaises(ValueError):channel.request('launch',{})
        self.assertEqual(calls,[])
        with self.assertRaises(UnknownRunnerOutcome):channel.ready()
        self.assertEqual(len(calls),1)


if __name__=='__main__':unittest.main()
