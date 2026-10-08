import csv
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from .actions import from_axes, to_axes
from .teacher import join_actions, prepare_actions


class ActionTests(unittest.TestCase):
    def test_v2_round_trip_keeps_real_axis_scale(self):
        for original in [(0.15,.10,0.), (-.3,0.,.6), (0.,0.,0.)]:
            self.assertEqual(to_axes(from_axes(*original)),original)
        self.assertEqual(from_axes(.15,.1,0.),(.15,.1))

    def test_overlapping_out_of_range_and_nonfinite_axes_rejected(self):
        for axes in [(0.,.1,.2),(2.,0.,0.),(0.,float('nan'),0.)]:
            with self.assertRaises(ValueError): from_axes(*axes)
        for action in [(0.,), (0.,float('inf')), (0.,-2.)]:
            with self.assertRaises(ValueError): to_axes(action)


class TeacherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.observation = dict(sequence='1',tracking_epoch='2',observation_ns='100',steering_raw='999')
        self.selection = dict(command_sequence=3,target_speed_kmh=10.)
        self.request = dict(sequence='3',observation_sequence='1',tracking_epoch='2',mode='Running',
                            reason='accepted',requested_steering='.15',requested_throttle='.1',
                            requested_brake='0.',created_ns='110',now_ns='120',expires_ns='200',
                            send_success='false',applied_steering='',applied_throttle='',applied_brake='')
        self.sent = dict(self.request,reason='running',now_ns='130',send_success='true',
                         applied_steering='.14',applied_throttle='.09',applied_brake='0.')
        self.patch = patch('TrainingEnvironment.teacher.joined_teacher',
                           return_value=({},[(self.observation,self.selection)],[]))
        self.patch.start();self.addCleanup(self.patch.stop)

    def write(self, rows):
        with (self.root/'commands.csv').open('w',encoding='utf-8',newline='') as stream:
            writer=csv.DictWriter(stream,fieldnames=list(self.request))
            writer.writeheader();writer.writerows(rows)

    def test_exact_request_and_transmission_are_separate_from_raw_steering(self):
        self.write([self.request,self.sent])
        _, rows, excluded = join_actions(self.root)
        self.assertFalse(excluded)
        self.assertEqual(rows[0]['action'],[.15,.1])
        self.assertEqual(rows[0]['transmitted_action'],[.14,.09])
        self.assertNotIn('steering_raw',rows[0]['observation'])
        self.assertEqual(rows[0]['transmitted_ns'],130)

    def test_steering_ambiguity_is_excluded_even_when_pedals_match(self):
        changed=dict(self.request,sequence='4',requested_steering='-.15')
        self.write([self.request,self.sent,changed])
        _,rows,excluded=join_actions(self.root)
        self.assertFalse(rows)
        self.assertEqual(excluded[-1]['reason'],'ambiguous_v2_action')

    def test_missing_transmission_is_not_fabricated(self):
        self.write([self.request])
        _,rows,excluded=join_actions(self.root)
        self.assertFalse(rows)
        self.assertEqual(excluded[-1]['reason'],'no_successful_transmission')

    def test_expired_or_mismatched_transmission_rejected(self):
        for sent in [dict(self.sent,now_ns='201'),dict(self.sent,now_ns='119'),
                     dict(self.sent,requested_steering='.2')]:
            self.write([self.request,sent])
            with self.assertRaises(ValueError): join_actions(self.root)

    def test_same_run_cannot_cross_split_boundaries(self):
        source=self.root/'sources.toml'
        source.write_text('schema_version=1\ntrain=["."]\nvalidation=["."]\nprepared="missing"',encoding='utf-8')
        with self.assertRaisesRegex(ValueError,'split boundaries'):
            prepare_actions(source,self.root/'output')
        self.assertFalse((self.root/'output').exists())

    @unittest.skipUnless(os.name == 'nt', 'Windows long-path alias regression')
    def test_windows_long_path_alias_cannot_cross_splits(self):
        source=self.root/'sources.toml'
        ordinary=str(self.root.resolve())
        alias='\\\\?\\'+ordinary
        source.write_text('schema_version=1\ntrain='+json.dumps([ordinary])+\
                          '\nvalidation='+json.dumps([alias])+'\nprepared="missing"',encoding='utf-8')
        with self.assertRaisesRegex(ValueError,'split boundaries'):
            prepare_actions(source,self.root/'output')


if __name__ == '__main__': unittest.main()
