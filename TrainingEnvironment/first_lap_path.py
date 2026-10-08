"""Explicit user-selected first-lap geometry, without a fastest-lap selection.

This is a single-demonstration path artifact, not a multi-lap review approval.
It retains the shared geometry limits and independently calibrates heading.
"""
import csv
from pathlib import Path
import time

from DrivingRecorder.validate_recording import validate
from DatasetBuilder.builder import csv_write
from ModelTrainer.common import new_folder, save, read, hashes, finish, verify_hashes
from TrackModel.config import check_config
from TrackModel.geometry import generate, FIELDS
from TrackModel.model import UNITS, COORDINATES, load_reference
from TrackModel.report import overlay
from StateProcessor.prepare import config_check, calibrate


def build(recording):
    source=Path(recording).resolve()
    proof=validate(source)
    if not proof['format_pass'] or proof['errors'] or proof['warnings']:
        raise ValueError('First-lap source must pass recording verification')
    lap=proof['laps'][0]
    if not lap['all_samples_lap_valid']:raise ValueError('First lap invalid')
    with (source/'telemetry.csv').open(newline='') as f:
        rows=[r for r in csv.DictReader(f) if int(r['segment_id'])==lap['segment_id']
              and lap['start_elapsed_ns']<=int(r['elapsed_ns'])<lap['end_elapsed_ns']]
    lid=source.name+'-first-lap'
    identity=read(source/'summary.json')['first_identity']
    inputs=hashes([source/k for k in ('metadata.json','telemetry.csv','events.csv','summary.json')]+[Path(__file__)])
    selection=new_folder('reinforcement_runs','first-lap-selection')
    selection_meta=dict(schema_version=1,kind='explicit_first_lap_selection',complete=False,
        input_sha256=inputs,selection='User requested first complete lap; later laps excluded',
        independent_multilap_review=False,lap=lap,lap_id=lid)
    save(selection/'manifest.json',selection_meta);save(selection/'validation.json',proof)
    finish(selection,selection_meta)
    inputs.update(hashes([selection/'manifest.json',selection/'validation.json']))
    # Half-metre sampling retains the first lap's tighter bends without raising
    # any smoothing-displacement, closure, spacing or tangent quality limits.
    config=check_config(dict(spacing_m=.5))
    geometry=generate([tuple(float(r[k]) for k in ('position_x','position_y','position_z')) for r in rows],config)
    if not geometry['validation']['passed']:raise ValueError('First-lap geometry failed existing limits')
    track=new_folder('track_models','first-lap')
    tm=dict(schema_version=1,tool_version='0.1.0',kind='track_model',complete=False,
        created_unix_ns=time.time_ns(),review_path=str(selection),selection_kind='explicit_user_first_lap',
        input_sha256=inputs,config=config,fields=FIELDS,units=UNITS,coordinates=COORDINATES,
        identity=identity,selected_lap=dict(lap_id=lid,ordinal=1,**lap),
        points=len(geometry['rows']),total_length_m=geometry['length_m'])
    save(track/'manifest.json',tm)
    csv_write(track/'reference.csv',FIELDS,geometry['rows'])
    save(track/'validation.json',dict(schema_version=1,input_pass=True,**geometry['validation']))
    raw=geometry['raw'];points=[tuple(r[k] for k in ('position_x','position_y','position_z')) for r in geometry['rows']]
    (track/'report.html').write_text('<!doctype html><meta charset="utf-8"><title>First lap path</title>'+overlay(raw,points,'First complete lap: recorded and smoothed path'),encoding='utf-8')
    finish(track,tm);load_reference(track)
    state_config=config_check({})
    heading=calibrate({lid:rows},[lid],identity,state_config)
    prepared=new_folder('state_runs','first-lap-prepared')
    inputs=inputs | hashes([track/k for k in ('manifest.json','reference.csv','validation.json','report.html')])
    meta=dict(schema_version=1,tool_version='0.1.0',kind='state_prepared',complete=False,
        created_unix_ns=time.time_ns(),track_model_path=str(track),review_path=str(selection),
        selection_kind='explicit_user_first_lap',input_sha256=inputs)
    save(prepared/'manifest.json',meta)
    save(prepared/'config.json',state_config);save(prepared/'heading_profile.json',heading)
    save(prepared/'replay.json',dict(source_path=str(source),identity=identity,
        laps=[dict(lap_id=lid,ordinal=1,split='train',start_ns=lap['start_elapsed_ns'],
                   end_ns=lap['end_elapsed_ns'],segment_id=lap['segment_id'])],
        eligible=dict(train=[lid],validation=[],test=[]),hold=[],exclude=[],
        diagnostic_policy='Only explicitly selected first lap; no independent validation claimed'))
    verify_hashes(inputs);finish(prepared,meta)
    print(prepared,flush=True)
    return prepared

if __name__=='__main__':
    import argparse
    p=argparse.ArgumentParser();p.add_argument('recording');a=p.parse_args();build(a.recording)
