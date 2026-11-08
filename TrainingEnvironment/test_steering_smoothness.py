import copy
import unittest
import torch
from .gamepad_online_learning import Learner,smoothness_config,smooth_pair,temporal_steering_loss,resume_smoothness
from .gamepad_online_data import SEMANTICS
from .outcome_learning import actor,scalar_network,predict,advantage_weights
from .steering_experiment import choose_candidate,identical

def fixture():
    torch.manual_seed(42);l=Learner.__new__(Learner);l.actor=actor()
    l.ao=torch.optim.Adam(l.actor.parameters(),lr=.0003)
    l.q1,l.q2,l.v=scalar_network(38),scalar_network(38),scalar_network(35)
    l.ic=dict(advantage_temperature=3.,maximum_actor_weight=20.,actor_rl_coefficient=.1)
    l.bc_x=torch.randn(32,35);l.bc_y=torch.rand(32,3);l.mask=torch.ones(32,3)
    l.weights=torch.ones(32,dtype=torch.double);l.bc_rng=torch.Generator().manual_seed(42)
    l.actor_updates=0;l.smoothness=smoothness_config()
    batch=(torch.randn(32,35),torch.randn(32,35),torch.rand(32,3),torch.zeros(32,1),
           torch.full((32,1),.02),torch.zeros(32,1),torch.ones(32,1,dtype=torch.bool))
    return l,batch

class SmoothnessTests(unittest.TestCase):
    def test_legacy_resume_defaults_off_and_change_requires_explicit_branch(self):
        self.assertEqual(resume_smoothness(None),dict(method='none',coefficient=0.))
        selected=dict(method='temporal_steering_v1',coefficient=1.)
        with self.assertRaisesRegex(ValueError,'explicit experiment branch'):resume_smoothness(None,selected)
        self.assertEqual(resume_smoothness(None,selected,True),selected)
        self.assertEqual(resume_smoothness(selected),selected)
        with self.assertRaises(ValueError):resume_smoothness(selected,dict(method='none',coefficient=0.))
    def test_steering_only_and_time_normalization(self):
        a=torch.tensor([[0.,.1,.8],[0.,.3,.2]],requires_grad=True)
        b=torch.tensor([[.2,.9,.1],[.2,.8,.8]],requires_grad=True)
        loss=temporal_steering_loss(a,b,torch.tensor([.02,.04]),torch.tensor([True,True]))
        self.assertAlmostEqual(loss.item(),.025,places=6);loss.backward()
        self.assertEqual(float(a.grad[:,1:].abs().sum()+b.grad[:,1:].abs().sum()),0.)
        self.assertGreater(float(a.grad[:,0].abs().sum()),0.)

    def test_no_invalid_pairs_or_terminal_reset(self):
        r=dict(continuous_semantics=SEMANTICS,terminated=False,truncated=False,source_log_sha256='a',tracking_epoch=1,
               observation_ns=10,send_end_ns=11,next_observation_ns=20_000_010,dt=.02,features=[0.]*35,next_features=[0.]*35)
        self.assertTrue(smooth_pair(r))
        l=Learner.__new__(Learner);l.mean=0.;l.std=1.;l.excluded=[]
        tensors=l.tensors([dict(r,action=[0.,0.,0.],reward=0.)])
        self.assertTrue(tensors[-1].item())
        for edit in [dict(terminated=True),dict(truncated=True),dict(next_tracking_epoch=2),dict(dt=0),dict(dt=float('nan')),
                     dict(send_end_ns=30_000_000),dict(dt=.2),dict(next_features=[float('nan')]*35),dict(source_log_sha256='')]:
            with self.subTest(edit=edit):self.assertFalse(smooth_pair(dict(r,**edit)))
        a=torch.zeros(2,3,requires_grad=True);b=torch.ones(2,3,requires_grad=True)
        self.assertEqual(temporal_steering_loss(a,b,torch.tensor([0.,float('nan')]),torch.tensor([False,True])).item(),0)

    def test_zero_coefficient_is_exact_legacy_actor_step_and_critics_frozen(self):
        original,batch=fixture();current=copy.deepcopy(original)
        x,n,a,r,dt,done,*_=batch;qa=torch.cat((x,a),1);ic=original.ic
        ids=torch.multinomial(original.weights,256,replacement=True,generator=original.bc_rng)
        bc=((predict(original.actor,original.bc_x[ids])-original.bc_y[ids]).square()*original.mask[ids]).sum()/original.mask[ids].sum()
        with torch.no_grad():w=advantage_weights(torch.minimum(original.q1(qa),original.q2(qa)),original.v(x),ic['advantage_temperature'],ic['maximum_actor_weight'])
        rl=(w.squeeze(1)*(predict(original.actor,x)-a).square().mean(1)).mean()
        loss=bc+ic['actor_rl_coefficient']*rl;original.ao.zero_grad();loss.backward();original.ao.step()
        critic=copy.deepcopy(current.q1.state_dict());current.actor_step(batch)
        self.assertTrue(identical(original.actor.state_dict(),current.actor.state_dict()))
        self.assertTrue(identical(original.ao.state_dict(),current.ao.state_dict()))
        self.assertTrue(identical(critic,current.q1.state_dict()))

    def test_actor_optimizer_rng_restore_reproduces_next_update(self):
        learner,batch=fixture();learner.smoothness=dict(method='temporal_steering_v1',coefficient=1.)
        learner.actor_step(batch);restored=copy.deepcopy(learner)
        a=learner.actor_step(batch);b=restored.actor_step(batch)
        self.assertEqual(a,b);self.assertTrue(identical(learner.actor.state_dict(),restored.actor.state_dict()))
        self.assertTrue(identical(learner.ao.state_dict(),restored.ao.state_dict()))

    def test_config_and_selection_fail_closed(self):
        self.assertEqual(smoothness_config()['coefficient'],0.)
        for v in [dict(method='other',coefficient=1),dict(method='none',coefficient=1),dict(method='temporal_steering_v1',coefficient=-1),dict(method='none',coefficient=True)]:
            with self.assertRaises(ValueError):smoothness_config(v)
        def row(c,s,m):return dict(coefficient=c,diagnostic=dict(temporal_loss=s,teacher_mse=[m]*3,pair_count=10))
        parent=row(0,1,1)['diagnostic'];candidates=[row(0,.9,1),row(.1,.8,1),row(1,.7,1),row(10,.4,2)]
        self.assertEqual(choose_candidate(candidates,parent)['coefficient'],1)

if __name__=='__main__':unittest.main()
