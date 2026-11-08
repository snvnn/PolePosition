import unittest
import torch
from .response_regularization import response_dropout


class ResponseDropoutTests(unittest.TestCase):
    def test_reproducible_training_only_copy(self):
        x=torch.arange(3500,dtype=torch.float32).reshape(100,35)+1
        original=x.clone();settings={'probability':.5,'columns':[25]}
        a=response_dropout(x,torch.Generator().manual_seed(42),settings)
        b=response_dropout(x,torch.Generator().manual_seed(42),settings)
        self.assertTrue(torch.equal(a,b));self.assertTrue(torch.equal(x,original))
        self.assertTrue(torch.equal(a[:,:25],x[:,:25]));self.assertTrue(torch.equal(a[:,26:],x[:,26:]))
        self.assertTrue(0<int((a[:,25]==0).sum())<100)

    def test_no_dropout_is_identity_and_rejects_nonresponse_columns(self):
        x=torch.ones(8,35);rng=torch.Generator().manual_seed(42)
        self.assertTrue(torch.equal(x,response_dropout(x,rng,{'probability':0})))
        for c in [{'probability':2},{'probability':float('nan')},{'probability':.5,'columns':[19]}]:
            with self.assertRaises(ValueError):response_dropout(x,rng,c)

if __name__=='__main__':unittest.main()
