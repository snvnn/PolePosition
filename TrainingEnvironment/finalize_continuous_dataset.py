"""Finalize rebuilt spatial/history provenance before any model consumes the dataset."""
import csv,json
from pathlib import Path
from ModelTrainer.common import read,save,finish,hashes,verify_hashes
from .augmentation import excluded


def finalize(experiment):
    experiment=Path(experiment);dataset=experiment/'dataset';m=read(dataset/'manifest.json')
    verify_hashes(m['output_sha256'],dataset)
    if m.get('provenance_rebuilt'):raise ValueError('Dataset already finalized')
    training=read(dataset/'training-pool.json');validation=read(dataset/'validation.json');sources=[]
    for i,path in enumerate(m['model_manifest']['recordings'],1):
        path=Path(path);folder=experiment/f'session-{i}';audit=read(folder/'alignment-review.json')
        breaks=set(audit['reference_seam_break_sequences']);windows=audit['exclusion_windows'];epoch=0;start=0;prev_segment=None;prev_excluded=False;contexts={}
        with (path/'telemetry.csv').open(newline='') as f:
            for row in csv.DictReader(f):
                ex=excluded(row,windows);seq=int(row['sequence'])
                if seq in breaks or row['segment_id']!=prev_segment or ex!=prev_excluded:epoch+=1;start=int(row['elapsed_ns'])
                contexts[seq]=(epoch,start);prev_segment=row['segment_id'];prev_excluded=ex
        with (folder/'frames.jsonl').open() as f:frames={r['frame']['sequence']:r['frame'] for line in f if (r:=json.loads(line))}
        for row in training+validation:
            if row['recording']!=str(path):continue
            frame=frames[row['sequence']];epoch,start=contexts[row['sequence']]
            if frame['tracking_epoch']!=epoch or frame['observation_ns']!=row['observation_ns']:raise ValueError('Rebuilt context differs')
            row['s_m']=frame['s_m'];row['tracking_epoch']=epoch;row['context_start_ns']=start
            if 'previous_input_end_ns' in row and row['previous_input_end_ns']<start:raise ValueError('Prior input crossed rebuilt context')
        sources.extend([folder/'frames.jsonl',folder/'features.jsonl',folder/'alignment-review.json'])
    save(dataset/'training-pool.json',training,replace=True);save(dataset/'validation.json',validation,replace=True)
    m['input_sha256'].update(hashes(sources+[Path(__file__)]));m['provenance_rebuilt']=True
    finish(dataset,m)
    print('FINALIZED',dataset,len(training),len(validation),flush=True)


if __name__=='__main__':
    import sys
    finalize(sys.argv[1])
