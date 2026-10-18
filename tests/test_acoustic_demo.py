"""声波演示的独立数学检查、闭环与断路反例。全部不依赖真实设备。"""
import cmath
import hashlib
import json
import math
from pathlib import Path
import tempfile
import unittest

from acoustic_demo.application import AcousticApplication, SCENARIO
from acoustic_demo.model import AcousticResponse, DemoModel, DATA
from acoustic_demo.server import AcousticServer
from sim2030.records import RunReader
from sim2030.presentation.server import PresentationServer


class TestAcousticMath(unittest.TestCase):
    def test_reference_integrity_and_normal_axis_scales(self):
        model = DemoModel()
        self.assertEqual(len(model.baseline), 16000)
        self.assertEqual(hashlib.sha256((DATA / "normal_xyz.csv.gz").read_bytes()).hexdigest(),
                         model.provenance["packaged_sha256"])
        for actual, expected in zip(model.scales, (590.2318785421852, 1469.4844148498241, 565.34793127288)):
            self.assertAlmostEqual(actual, expected, places=8)
        model.sample_to(1.01)
        for row in model.last_wave:
            self.assertEqual(row[2:5], row[8:11])
            self.assertEqual(row[5:8], [0, 0, 0])

    def test_continuous_response_matches_frequency_domain(self):
        response = AcousticResponse()
        response.switch(.3, 0)
        w0, wc, w = 2 * math.pi * 5500, 2 * math.pi * 1500, 2 * math.pi * 5500
        transfer = w0*w0 / ((1j*w)**2 + w0/17*1j*w + w0*w0) * wc / (1j*w+wc)
        for t in (.20, .20025, .201, .2025):
            expected = (.3 * transfer * cmath.exp(1j*w*t)).real
            self.assertAlmostEqual(response.value(t)[1], expected, places=8)

    def test_analytic_transients_agree_with_independent_rk4(self):
        # 独立地对三个实数微分方程做 RK4，不复用解析解的极点/余数。
        response = AcousticResponse()
        response.switch(.3, 0)
        response.switch(0, .03)
        w0, wc = 2 * math.pi * 5500, 2 * math.pi * 1500
        def derivative(t, state):
            envelope = (.5-.5*math.cos(math.pi*t/.02)) if t < .02 else 1
            if t >= .03:
                envelope = (.5+.5*math.cos(math.pi*(t-.03)/.02)) if t < .05 else 0
            u = .3 * envelope * math.cos(w0*t)
            x, velocity, output = state
            return velocity, w0*w0*(u-x)-w0/17*velocity, wc*(x-output)
        state, dt, errors, references = (0, 0, 0), 1/256000, [], []
        for index in range(round(.065 / dt)):
            t=index*dt
            a=derivative(t,state)
            b=derivative(t+dt/2,tuple(s+dt*v/2 for s,v in zip(state,a)))
            c=derivative(t+dt/2,tuple(s+dt*v/2 for s,v in zip(state,b)))
            d=derivative(t+dt,tuple(s+dt*v for s,v in zip(state,c)))
            state=tuple(s+dt*(a0+2*b0+2*c0+d0)/6 for s,a0,b0,c0,d0 in zip(state,a,b,c,d))
            if (index+1)%64==0:
                exact=response.value((index+1)*dt)[1]
                errors.append((state[2]-exact)**2)
                references.append(exact**2)
        self.assertLess(math.sqrt(sum(errors)/sum(references)), .002)

    def test_xyz_are_additive_and_original_amplitude_is_preserved(self):
        model=DemoModel()
        model.response.switch(.3, 1)
        features=model.sample_to(3)
        # 旧 TI01_S CSV 在 [1,2) 秒的三轴统计；解析解相对旧数值积分略有差异。
        for actual, expected in zip(features[1]["rms_ratio_xyz"],
                                    (1.3757690829, 1.1387741312, 1.0170288140)):
            self.assertAlmostEqual(actual, expected, delta=.002)
        for row in model.last_wave:
            for i in range(3):
                self.assertAlmostEqual(row[8+i],row[2+i]+row[5+i],places=9)
            # 三轴共享参考响应，各除以自己的基准尺度后，比例严格为权重。
            normalized=[row[5+i]/model.scales[i] for i in range(3)]
            self.assertAlmostEqual(normalized[1],.6*normalized[0],places=10)
            self.assertAlmostEqual(normalized[2],.3*normalized[0],places=10)

    def test_thermal_balance_and_inertial_coast(self):
        model=DemoModel()
        model.advance(20)
        self.assertAlmostEqual(model.oil_c,55,places=9)
        model.set_power(False,20)
        model.advance(21)
        self.assertAlmostEqual(model.current_a,2*math.exp(-4),places=9)
        self.assertAlmostEqual(model.speed_ratio,math.exp(-.2),places=9)
        self.assertGreater(model.oil_c,55)
        self.assertLess(model.oil_sensor_c,model.oil_c)
        model.advance(1820)
        self.assertGreater(model.oil_c,60)
        self.assertLess(model.oil_c,85)  # 停机后仍有自然散热，平衡温度 85 ℃。


class TestAcousticLoop(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.app=AcousticApplication(self.tmp.name)
        self.app.load(str(SCENARIO))
        self.app.start()

    def tearDown(self):
        self.app.close()
        self.tmp.cleanup()

    def advance(self,seconds):
        for _ in range(round(seconds*10)):
            self.app.step()

    def source(self,action):
        return self.app.submit_operation({"target_asset_id":"sensor_attack_node","action_type":action})

    def test_normal_and_original_amplitude_do_not_trip(self):
        self.advance(4)
        self.assertIsNone(self.app.acoustic.cutoff_s)
        self.source("start_weak")
        self.advance(10)
        self.assertTrue(self.app.acoustic.powered)
        self.assertFalse(self.app.acoustic.trip_latched)
        self.assertLess(self.app.acoustic.feature["max_ratio"],2)

    def test_strong_trip_uses_three_received_windows_and_round_trip_path(self):
        self.advance(3)
        self.source("start_strong")
        self.advance(12)
        self.assertFalse(self.app.acoustic.powered)
        self.assertTrue(self.app.acoustic.trip_latched)
        trip=next(e for e in self.app.events if e["kind"]=="threshold_trip")
        issued=next(e for e in self.app.events if e["kind"]=="stop_command_issued")
        self.assertAlmostEqual(issued["observed_time_s"]-trip["observed_time_s"],1)
        self.assertGreater(self.app.acoustic.cutoff_s,issued["observed_time_s"])
        messages=[e for e in self.app.events if e["kind"]=="message_delivered"]
        commands=[(e['src'],e['dst']) for e in messages if e['business_type']=='command']
        self.assertEqual(commands,[("station","mc"),("mc","ctl_cool"),("ctl_cool","cool")])
        completed=[(e['src'],e['dst']) for e in messages if e['payload'].get('status')=='completed']
        self.assertEqual(completed,[("cool","ctl_cool"),("ctl_cool","mc"),("mc","station")])
        self.assertEqual({e['request_id'] for e in messages},{trip['request_id']})
        self.app.recorder.flush()
        reader=RunReader(str(self.app.recorder.output_dir))
        observed=[e for e in reader.read_all('acoustic_path') if e['kind']=='measurement_received' and e['observed_time_s']<=trip['observed_time_s']]
        self.assertEqual([e['consecutive_windows'] for e in observed[-3:]],[1,2,3])
        self.assertTrue(any(e['kind']=='oil_measurement_received' for e in reader.read_all('acoustic_path')))

    def test_blocking_control_link_prevents_cutoff_despite_alarm(self):
        self.app.engine._network.set_link_effect('L-mc-cool',{'extra_loss_rate':1})
        self.source('start_strong')
        self.advance(10)
        self.assertTrue(self.app.acoustic.trip_latched)
        self.assertTrue(self.app.acoustic.powered)
        self.assertIsNone(self.app.acoustic.cutoff_s)
        self.assertFalse(any(e.get('dst')=='cool' for e in self.app.events))

    def test_missing_fan_telemetry_prevents_threshold_decision(self):
        self.app.engine._network.set_link_effect('L-fan-monitor-mc',{'extra_loss_rate':1})
        self.source('start_strong')
        self.advance(10)
        self.assertGreater(self.app.acoustic.feature['max_ratio'],2)
        self.assertFalse(self.app.acoustic.trip_latched)
        self.assertTrue(self.app.acoustic.powered)

    def test_source_off_does_not_restart_and_reset_requires_normal_window(self):
        self.source('start_strong')
        self.advance(10)
        self.assertEqual(self.source('reset_protection')['status'],'rejected')
        self.source('stop_attack')
        self.advance(3)
        self.assertFalse(self.app.acoustic.powered)
        preview=self.app.get_view('acoustic')['attack_preview']
        self.assertIsNotNone(preview)
        self.assertLess(preview['time_s'],self.app.acoustic.cutoff_s)
        self.assertEqual(len(preview['wave']),80)
        self.assertTrue(any(abs(row[5])>1 for row in preview['wave']))
        self.app.submit_operation({'target_asset_id':'cool','action_type':'start'})
        self.advance(1)
        self.assertFalse(self.app.acoustic.powered)
        self.assertEqual(self.source('reset_protection')['status'],'accepted')
        self.app.submit_operation({'target_asset_id':'cool','action_type':'start'})
        self.advance(1)
        self.assertTrue(self.app.acoustic.powered)

    def test_old_scene_still_loads_in_extension_application(self):
        self.app.load(str(SCENARIO.parent.parent/'scenarios/substation-sensor-attack.json'))
        self.assertIsNone(self.app.acoustic)
        self.app.start()
        self.app.step()
        self.assertFalse(self.app.get_view('acoustic')['available'])


class TestAcousticServer(unittest.TestCase):
    def test_acoustic_entry_lists_only_its_scene_without_changing_shared_server(self):
        with tempfile.TemporaryDirectory() as folder:
            server=AcousticServer(output_root=folder)
            shared=PresentationServer(output_root=folder)
            try:
                status,payload,_=server.handle_request('GET','/api/scenarios')
                self.assertEqual(status,200)
                self.assertEqual([(s['scenario_id'],s['name']) for s in payload['scenarios']],
                                 [('substation-acoustic-demo','声波干扰联动（仿真）')])
                self.assertIn('substation-em-attack',shared._scenario_paths)
                self.assertIn('substation-network-attack',shared._scenario_paths)
                self.assertNotIn('substation-acoustic-demo',shared._scenario_paths)
            finally:
                server.close()
                shared.close()

    def test_addon_routes_and_bound_advancement(self):
        with tempfile.TemporaryDirectory() as folder:
            server=AcousticServer(output_root=folder)
            try:
                status,payload,_=server.handle_request('GET','/')
                self.assertEqual(status,200)
                self.assertIn('data-view="acoustic"',payload['__raw__'])
                self.assertIn('/acoustic.js',payload['__raw__'])
                self.assertEqual(server.handle_request('GET','/acoustic.js')[0],200)
                def post(route,body):
                    return server.handle_request('POST',route,json.dumps(body).encode())
                self.assertEqual(post('/api/acoustic/advance',{'seconds':61})[0],400)
                self.assertEqual(post('/api/acoustic/advance',{'seconds':1,'run_id':'stale'})[0],409)
                _,created,_=post('/api/runs',{'scenario_id':'substation-acoustic-demo'})
                status,payload,_=post('/api/acoustic/advance',{'seconds':2,'run_id':created['run_id']})
                self.assertEqual(status,200)
                self.assertAlmostEqual(payload['data']['time_s'],2)
                self.assertEqual(server.application.status,'paused')
                self.assertTrue(payload['data']['powered'])
            finally:
                server.close()


if __name__=='__main__':
    unittest.main()
