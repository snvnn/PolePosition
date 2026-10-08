import copy
import unittest
from .corrections import label
from .gamepad import masked_mse

class CorrectionLabels(unittest.TestCase):
    def fixture(self):
        return dict(authority='human_steer',observation_ns=100,now_ns=120,features=[0.]*35,
                    action=[-.1,.8,.2],selected_action=[1.,.8,.2],label_mask=[1,0,0],
                    teacher=dict(connected=True,normalization_version=1,read_start_ns=101,read_end_ns=102,lx=32767,rt=0,lt=255,buttons=0x100))
    def test_only_human_axes_are_labels(self):
        p=self.fixture();r=label(p);self.assertEqual(r['action'],[1,0,1]);self.assertEqual(r['mask'],[1,0,0])
        p['authority']='human_full';p['teacher']['buttons']=0x300;p['selected_action']=[1,0,1];p['label_mask']=[1,1,1]
        self.assertEqual(label(p)['mask'],[1,1,1])
        p['authority']='model';self.assertIsNone(label(p))
    def test_no_future_labels_emergency_or_wrong_authority(self):
        for change in [dict(read_start_ns=99),dict(read_end_ns=121),dict(connected=False),dict(buttons=0x120),dict(buttons=0x200)]:
            p=self.fixture();p['teacher'].update(change)
            with self.subTest(change=change),self.assertRaises(ValueError):label(p)
    def test_uncontrolled_pedals_have_zero_target_gradient(self):
        import torch
        y=torch.tensor([[.1,.5,.5]],requires_grad=True)
        masked_mse(y,torch.tensor([[1.,0.,1.]]),torch.tensor([[1.,0.,0.]])).backward()
        self.assertNotEqual(y.grad[0,0].item(),0)
        self.assertEqual(y.grad[0,1:].tolist(),[0,0])

if __name__=='__main__':unittest.main()
