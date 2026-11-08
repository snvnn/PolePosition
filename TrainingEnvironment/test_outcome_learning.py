import unittest
import numpy as np
import torch
from .outcome_data import confirmed_failure,start_cut,reward_components
from .outcome_learning import advantage_weights,expectile_loss,bellman_target


def observation(t,seq,valid=True,epoch=1,x=0):
    return dict(kind='observation',observation_ns=t,sequence=seq,state=dict(status='ready',tracking_epoch=epoch),
                sample=dict(lap_valid=valid,session_index=0,position_x=x,position_z=0))


class Outcomes(unittest.TestCase):
    def test_failure_requires_before_stop_continuous_validity_change(self):
        running=dict(kind='sent',mode='Running',send_start_ns=1,send_end_ns=2)
        stop=dict(kind='sent',mode='Stopping',send_start_ns=30_000_000,send_end_ns=30_000_001)
        a,b=observation(10_000_000,1),observation(20_000_000,2,False)
        self.assertEqual(confirmed_failure([running,a,b,stop])['sequence'],2)
        self.assertIsNone(confirmed_failure([running,a,observation(40_000_000,2,False),stop]))
        self.assertIsNone(confirmed_failure([running,a,observation(20_000_000,2,False,epoch=2),stop]))
        self.assertIsNone(confirmed_failure([running,a,observation(20_000_000,2,False,x=100),stop]))
        self.assertIsNone(confirmed_failure([running,a,observation(20_000_000,3,False),stop]))
        self.assertIsNone(confirmed_failure([running,a,observation(20_000_000,2),stop]))

    def test_start_quarantine_is_not_continuous_crossing_quarantine(self):
        self.assertEqual(start_cut([(0,990),(10,5),(20,151)],1000,150,620),640)
        self.assertIsNone(start_cut([(0,700),(10,990),(20,5)],1000,150,620))
        self.assertEqual(start_cut([(0,10),(20,30)],1000,150,620),640)

    def test_terminal_reward_and_reverse_progress(self):
        c=dict(progress=1,time=-.1,lateral_squared=-.2,heading_squared=-.1,applied_action_change_squared=-.01,invalid_lap=-10,valid_full_lap=10)
        r=reward_components([5,2,.1],[4,2,.1],np.zeros(3),[0,1,0],.02,'invalid_lap',c)
        self.assertEqual(r['progress'],-1);self.assertEqual(r['terminal'],-10)
        self.assertAlmostEqual(r['lateral'],-.016);self.assertAlmostEqual(r['input_change'],-.01)

    def test_bootstrap_time_limit_and_terminal(self):
        r=torch.tensor([[1.],[1.]]);dt=torch.tensor([[.02],[.04]])
        out=bellman_target(r,dt,torch.tensor([[1.],[0.]]),torch.ones(2,1)*3,.9)
        self.assertAlmostEqual(out[0].item(),1);self.assertAlmostEqual(out[1].item(),3.43,places=5)

    def test_weights_are_nonnegative_capped_and_bad_action_downweighted(self):
        w=advantage_weights(torch.tensor([-2.,0.,2.,100.]),torch.zeros(4),3.,20.)
        self.assertTrue(torch.all(w>=0));self.assertLess(w[0],w[1]);self.assertAlmostEqual(w[-1].item(),20,places=4)
        self.assertFalse(w.requires_grad)
        self.assertAlmostEqual(expectile_loss(torch.tensor([2.,-2.]),.7).item(),2.)


if __name__=='__main__':unittest.main()
