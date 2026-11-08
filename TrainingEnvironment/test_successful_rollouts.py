import copy
from pathlib import Path
import unittest
from .successful_rollouts import example_from_pair, set_mixture_weights, validate_segment


class SuccessfulRolloutTests(unittest.TestCase):
    def setUp(self):
        self.o=dict(observation_ns=100,sequence=7,sample=dict(lap_valid=True),
                    state=dict(status='ready',s_m=700.,tracking_epoch=1))
        self.p=dict(mode='Running',authority='model',label_mask=[0,0,0],features=[0.]*35,
                    action=[-.5,1.,.2],selected_action=[-.5,1.,.2],command_action=[-.5,1.,.2],observation_ns=100,now_ns=200)
        report=dict(lx=-16384,rt=255,lt=51)
        self.sent=dict(mode='Running',authority='model',send_start_ns=300,send_end_ns=400,
                       report=report,xinput=[1,dict(report)])

    def extract(self):
        return example_from_pair(self.o,self.p,self.sent,archive=Path('archive'),lap=2,source_model='abc')

    def test_actual_policy_features_and_three_actions_preserved_with_provenance(self):
        result=self.extract()
        self.assertEqual(result['features'],self.p['features'])
        self.assertEqual(result['action'],[-.5,1.,.2])
        self.assertEqual(result['source_kind'],'autonomous_success')
        self.assertEqual(result['lap'],2)
        result['features'][0]=5
        self.assertEqual(self.p['features'][0],0)

    def test_rejects_human_stop_invalid_lag_override_nonfinite_and_transport(self):
        changes=[lambda:self.p.update(authority='human_full'),lambda:self.sent.update(mode='Stopping'),
                 lambda:self.o['sample'].update(lap_valid=False),lambda:self.p.update(label_mask=[1,0,0]),
                 lambda:self.sent.update(send_end_ns=30_000_000),lambda:self.p.update(command_action=[0,1,.2]),
                 lambda:self.p['features'].__setitem__(0,float('nan')),
                 lambda:self.sent['report'].update(lx=16000),lambda:self.sent['xinput'][1].update(lt=0)]
        for mutate in changes:
            with self.subTest(change=mutate):
                self.setUp();mutate()
                with self.assertRaises(ValueError):self.extract()

    def test_mixture_preserves_importance_weights_and_source_fraction(self):
        human=[dict(weight=1.),dict(weight=2.)];success=[dict(weight=1.),dict(weight=2.)]
        before=copy.deepcopy(human)
        set_mixture_weights(human,success,.1)
        self.assertEqual(human,before)
        self.assertAlmostEqual(success[1]['weight']/success[0]['weight'],2.)
        mass=sum(r['weight'] for r in success)
        self.assertAlmostEqual(mass/(mass+3),.1)

    def test_full_lap_allows_only_bounded_finish_projection_wrap(self):
        chunk=[dict(s_m=float(i),observation_ns=i*20_000_000) for i in range(301)]
        self.assertEqual(validate_segment(chunk,0,300,True),0)
        chunk[-1]['s_m']=0.
        self.assertEqual(validate_segment(chunk,0,300,True),1)
        with self.assertRaises(ValueError):validate_segment(chunk,0,300,False)
        chunk[150]['s_m']=0.
        with self.assertRaises(ValueError):validate_segment(chunk,0,300,True)

    def test_full_lap_rejects_incomplete_tail_or_time_gap(self):
        chunk=[dict(s_m=float(i),observation_ns=i*20_000_000) for i in range(290)]
        with self.assertRaises(ValueError):validate_segment(chunk,0,300,True)
        chunk.extend(dict(s_m=float(i),observation_ns=i*20_000_000) for i in range(290,301))
        chunk[150]['observation_ns']+=100_000_000
        with self.assertRaises(ValueError):validate_segment(chunk,0,300,True)

    def test_reprojected_lap_may_cross_reference_origin_just_after_acc_boundary(self):
        chunk=[dict(s_m=float((i-1)%300),observation_ns=i*20_000_000) for i in range(301)]
        self.assertEqual(validate_segment(chunk,0,300,True),1)


if __name__=='__main__':unittest.main()
