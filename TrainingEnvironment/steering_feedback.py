"""Causal ACC steering observation; raw gamepad targets remain untouched."""
import csv, math
from pathlib import Path

VARIANT='acc_steering_feedback_v1'


def attach_feedback(rows):
    by_record={}
    for row in rows:by_record.setdefault(row['recording'],{})[row['sequence']]=row
    for recording,wanted in by_record.items():
        found=set()
        with (Path(recording)/'telemetry.csv').open(newline='',encoding='utf-8') as f:
            for observation in csv.DictReader(f):
                sequence=int(observation['sequence'])
                if sequence not in wanted:continue
                row=wanted[sequence]
                if sequence in found or int(observation['elapsed_ns'])!=row['observation_ns']:
                    raise ValueError('Feedback observation time/sequence mismatch')
                value=float(observation['steering_raw'])
                if not math.isfinite(value):raise ValueError('Nonfinite steering feedback')
                row['features'][3]=value
                found.add(sequence)
        if found!=set(wanted):raise ValueError('Missing causal steering feedback')
