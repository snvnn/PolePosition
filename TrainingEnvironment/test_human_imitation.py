import unittest
import numpy as np
from .human_imitation import project, select_first_lap


class HumanImitationTests(unittest.TestCase):
    def test_selection_includes_approach_but_never_second_lap(self):
        rows=[dict(segment_id='1',elapsed_ns=i*10_000_000) for i in range(301)]
        report=dict(format_pass=True,errors=[],warnings=[],laps=[dict(segment_id=1,
            start_elapsed_ns=500_000_000,end_elapsed_ns=2_000_000_000,all_samples_lap_valid=True)])
        selected,_=select_first_lap(rows,report)
        self.assertEqual(len(selected),201)
        self.assertEqual(selected[0]['elapsed_ns'],0)
        self.assertEqual(selected[-1]['elapsed_ns'],2_000_000_000)
        report['laps'][0]['all_samples_lap_valid']=False
        with self.assertRaises(ValueError): select_first_lap(rows,report)

    def test_projection_has_native_lateral_and_heading_convention(self):
        reference=[dict(position_x=x,position_z=z,tangent_x=tx,tangent_z=tz,s_m=s)
                   for x,z,tx,tz,s in [(0,0,1,0,0),(10,0,1,0,10),(10,10,0,1,20),(0,10,-1,0,30)]]
        rows=[dict(position_x=x,position_z=1,heading_rad=-np.pi/2,
                   speed_kmh=36,elapsed_ns=i*10_000_000) for i,x in enumerate([2,2.1,2.2])]
        g=project(rows,reference,40,dict(sign=1,offset_rad=np.pi/2))
        np.testing.assert_allclose(g[:,0],[2,2.1,2.2])
        np.testing.assert_allclose(g[:,1],1)
        np.testing.assert_allclose(g[:,2],0)
