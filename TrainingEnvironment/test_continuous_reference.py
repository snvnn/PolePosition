import math,unittest
import numpy as np
from TrackModel.geometry import path_rows
from .continuous_reference import fast_project,bridge_points
from .human_imitation import project


class ContinuousReferenceTests(unittest.TestCase):
    def setUp(self):
        pts=[(100*math.cos(i*2*math.pi/1260),0.,100*math.sin(i*2*math.pi/1260)) for i in range(1260)]
        self.ref,self.length=path_rows(pts,5)
        self.rows=[]
        for j,i in enumerate(list(range(1230,1260))+list(range(30))):
            p=self.ref[i]
            self.rows.append(dict(position_x=p['position_x']*1.01,position_z=p['position_z']*1.01,
                heading_rad=p['heading_path_rad'],elapsed_ns=(j+1)*20_000_000,speed_kmh=100,segment_id=1))
        self.heading=dict(sign=1,offset_rad=0)

    def test_spatial_projection_matches_exhaustive_projection_through_wrap(self):
        expected=project(self.rows,self.ref,self.length,self.heading,True)
        actual=fast_project(self.rows,self.ref,self.length,self.heading,True)
        np.testing.assert_allclose(actual,expected,atol=1e-10,rtol=0)

    def test_projection_rejects_distant_pose(self):
        self.rows[0]['position_x']=500
        with self.assertRaises(ValueError):fast_project(self.rows,self.ref,self.length,self.heading,True)

    def test_bridge_changes_only_local_coordinates(self):
        points,displacement=bridge_points(self.ref,self.length)
        self.assertTrue(np.isfinite(points).all())
        for i,p in enumerate(self.ref):
            if min(p['s_m'],self.length-p['s_m'])>30:self.assertEqual(displacement[i],0)
        self.assertLess(displacement.max(),.02)


if __name__=='__main__':unittest.main()
