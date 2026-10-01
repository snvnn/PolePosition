"""Check the review chain and reproduce eligible train membership."""
from pathlib import Path

from DatasetBuilder.review import (VERSION as REVIEW_VERSION, decision_usage, digest,
                                   load_dataset, read_json, snapshot, verify_snapshot)


def input_locations(review):
    review = Path(review).resolve()
    final = read_json(review/'manifest.json')
    scan = Path(final['review_path']).resolve()
    dataset = Path(read_json(scan/'manifest.json')['dataset_path']).resolve()
    source = Path(read_json(dataset/'manifest.json')['source_path']).resolve()
    return review, scan, dataset, source


def load_review(review):
    review, scan, dataset, source = input_locations(review)
    before = snapshot([review/name for name in ('manifest.json','decisions.json','usage.json')])
    final, inspection = read_json(review/'manifest.json'), read_json(scan/'manifest.json')
    for meta, kind in ((final,'final'), (inspection,'scan')):
        if (meta.get('schema_version') != 1 or meta.get('review_version') != REVIEW_VERSION or
                meta.get('complete') is not True or meta.get('kind') != kind):
            raise ValueError('TrackModel requires a complete supported final review and scan')
    verify_snapshot(final['input_sha256'])
    verify_snapshot(final['review_sha256'])
    if final['input_sha256'] != inspection['input_sha256']:
        raise ValueError('Final and scan input fingerprints disagree')
    names = {'baselines.json','findings.json','report.html'}
    if set(inspection['artifact_sha256']) != names:
        raise ValueError('Missing scan artifact fingerprints')
    required = {str(scan/name): checksum for name,checksum in inspection['artifact_sha256'].items()}
    required[str(scan/'manifest.json')] = digest(scan/'manifest.json')
    if any(final['review_sha256'].get(k) != v for k,v in required.items()):
        raise ValueError('Final scan fingerprints disagree')
    decision_inputs = set(final['review_sha256'])-required.keys()
    if len(decision_inputs) != 1:
        raise ValueError('Missing original decision fingerprint')
    decisions = read_json(review/'decisions.json')
    if decisions != read_json(Path(next(iter(decision_inputs)))):
        raise ValueError('Final decision copy differs from reviewed input')
    data = load_dataset(dataset)
    if data['input_sha256'] != final['input_sha256']:
        raise ValueError('Dataset fingerprint inventory disagrees')
    findings = read_json(scan/'findings.json')
    accepted = [lap for lap in data['laps'] if lap['accepted']]
    if len(findings['laps']) != len(accepted):
        raise ValueError('Review lap inventory disagrees')
    for found, lap in zip(findings['laps'], accepted):
        expected = dict(lap_id=lap['lap_id'], ordinal=int(lap['ordinal']), split=lap['split'],
                        samples=int(lap['samples']), duration_seconds=float(lap['duration_seconds']))
        if any(found.get(k) != v for k,v in expected.items()):
            raise ValueError('Reviewed lap differs from dataset')
    usage = decision_usage(findings, decisions)
    if usage != read_json(review/'usage.json'):
        raise ValueError('Usage list disagrees with reviewed decisions')
    if (final['counts'] != {k:len(v) for k,v in usage['eligible'].items()} or
            final['hold_count'] != len(usage['hold']) or final['exclude_count'] != len(usage['exclude']) or
            final['trackmodel_ready'] != usage['trackmodel_ready']):
        raise ValueError('Final review counts disagree')
    inputs = before | final['input_sha256'] | final['review_sha256']
    verify_snapshot(inputs)
    return dict(data=data, usage=usage, input_sha256=inputs,
                locations=[review,scan,dataset,source])


def select_lap(laps, eligible_train):
    if not eligible_train or len(set(eligible_train)) != len(eligible_train):
        raise ValueError('No eligible train laps or duplicate candidate IDs')
    index = {lap['lap_id']:lap for lap in laps}
    candidates = []
    for lid in eligible_train:
        lap = index.get(lid)
        if lap is None or lap['split'] != 'train' or lap['accepted'] is not True:
            raise ValueError('Candidate is not an accepted train lap')
        start, end = int(lap['start_elapsed_ns']), int(lap['end_elapsed_ns'])
        if end <= start:
            raise ValueError('Invalid lap duration')
        candidates.append(dict(lap_id=lid, ordinal=int(lap['ordinal']), start_elapsed_ns=start,
                               duration_ns=end-start, duration_seconds=(end-start)/1e9))
    candidates.sort(key=lambda lap:(lap['duration_ns'],lap['start_elapsed_ns'],lap['lap_id']))
    return candidates[0], candidates
