import copy,csv,tempfile,unittest
from pathlib import Path
from .steering_feedback import attach_feedback

class FeedbackTests(unittest.TestCase):
    def test_same_observation_only_and_unchanged_targets(self):
        with tempfile.TemporaryDirectory() as tmp:
            with (Path(tmp)/'telemetry.csv').open('w',newline='') as f:
                w=csv.writer(f);w.writerow(['sequence','elapsed_ns','steering_raw'])
                w.writerows([[1,10,.1],[2,20,-.4],[3,30,.8]])
            row={'recording':tmp,'sequence':2,'observation_ns':20,'features':[1.]*35,'action':[-.7,.3,.2]}
            before=copy.deepcopy(row);attach_feedback([row])
            self.assertEqual(row['features'][3],-.4)
            self.assertEqual(row['action'],before['action'])
            self.assertEqual(row['features'][:3]+row['features'][4:],before['features'][:3]+before['features'][4:])
            row['observation_ns']=21
            with self.assertRaises(ValueError):attach_feedback([row])
            row['sequence']=4;row['observation_ns']=40
            with self.assertRaises(ValueError):attach_feedback([row])

if __name__=='__main__':unittest.main()
