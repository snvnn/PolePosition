"""Synthetic closed-track source for geometry and state replay tests."""
import math
from DatasetBuilder.builder import REQUIRED, csv_write, json_write

def circle(radius=50, count=720, irregular=False):
    return [(radius*math.cos(2*math.pi*(i/count)**(1.15 if irregular else 1)),
             3*math.sin(2*math.pi*i/count),
             radius*math.sin(2*math.pi*(i/count)**(1.15 if irregular else 1))) for i in range(count)]


def make_source(folder):
    rows=[]
    # Eight finish crossings, seven full laps, identical circles, default 100ms gaps.
    for i in range(481):
        phase=(i%60)/60;elapsed=i*100_000_000
        row=dict.fromkeys(REQUIRED,0)
        row.update(sequence=i+1,segment_id=1,elapsed_ns=elapsed,received_unix_ns=elapsed,
                   interval_ns=100_000_000 if i else 0,physics_changed_ns=elapsed,graphics_changed_ns=elapsed,
                   physics_packet_id=i,graphics_packet_id=i,speed_kmh=113,progress=phase,
                   position_x=30*math.cos(2*math.pi*phase),position_z=30*math.sin(2*math.pi*phase),
                   completed_laps=i//60,lap_time_ms=(i%60)*100,sector=int(phase*3),lap_valid='true',in_pit='false')
        rows.append(row)
    events=[dict(elapsed_ns=0,segment_id=1,kind='segment_start',detail='start')]
    events += [dict(elapsed_ns=t*1_000_000_000,segment_id=1,kind='lap_boundary',detail='lap') for t in range(6,49,6)]
    json_write(folder/'metadata.json',dict(schema_version=1,session_id='track-test',config=dict(stale_seconds=2)))
    json_write(folder/'summary.json',dict(schema_version=1,session_id='track-test',normal_shutdown=True,
               received_samples=len(rows),written_samples=len(rows),dropped_samples=0,dropped_events=0,
               written_events=len(events),read_timing={},poll_timing={}))
    csv_write(folder/'telemetry.csv',REQUIRED,rows)
    csv_write(folder/'events.csv',['elapsed_ns','segment_id','kind','detail'],events)
