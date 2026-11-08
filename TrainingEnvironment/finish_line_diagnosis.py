"""Analyze actual finish-line passes and frozen-feature sensitivity, without driving."""
import json, math
from pathlib import Path
import numpy as np
from ModelTrainer.common import read, save, hashes, finish, new_folder, verify_hashes

LATEST=['gamepad-1793424022562561400-52032','gamepad-1793424064023111200-5944']
ARCHIVE=Path('reinforcement_runs/autonomous-two-laps-1793422357204916300-c70c929b')
OLD=Path('reinforcement_runs/gamepad-imitation-1792231181059399600-43934c30')
NEW=Path('reinforcement_runs/gamepad-imitation-1793423783910919400-7e63824b')
LENGTH=3882.316161489197


def main():
    from GamepadIO.run_report import inspect_log
    from .augmentation_experiment import predict
    evidence=read(ARCHIVE/'successful-laps.json');verify_hashes(evidence['evidence_sha256'])
    specs=[(n,Path('actuator_runs')/n,None) for n in LATEST]
    specs += [(f'success_lap_{lap["lap"]}_finish',ARCHIVE/'source-run',lap['end_observation_ns']) for lap in evidence['successful_full_laps']]
    series=[];checks=[];inputs=[Path(__file__)];latest=None
    for name,folder,crossing in specs:
        m=read(folder/'manifest.json')
        if folder!=ARCHIVE/'source-run':verify_hashes(m['output_sha256'],folder)
        inputs.extend([folder/'manifest.json',folder/'samples.jsonl'])
        rows=[json.loads(s) for s in (folder/'samples.jsonl').read_text().splitlines()]
        transport,pairs=inspect_log(rows)
        if not transport['passed']:raise ValueError(transport)
        ps=[p for _,p,_ in pairs];origin=ps[0]['observation_ns'];observations={r['observation_ns']:r for r in rows if r['kind']=='observation'}
        data=[]
        for p in ps:
            o=observations[p['observation_ns']];s=o['state']['s_m'];q=s-LENGTH if s>LENGTH/2 else s
            if not -250<=q<=110 or (crossing is not None and abs(p['observation_ns']-crossing)>5_000_000_000):continue
            if p['authority']!='model' or any(p['label_mask']):raise ValueError('Human-controlled comparison')
            f=p['features']
            data.append(dict(s=q,t=(p['observation_ns']-origin)/1e9,features=f,action=p['action'],
                acc_steer=o['sample']['steering_raw'],position=[o['sample'][k] for k in ('position_x','position_z')],
                reference_heading=o['state']['vehicle_heading_rad']-math.atan2(f[5],f[6]),tracking_epoch=o['state']['tracking_epoch']))
        series.append(dict(name=name,data=data))
        checks.append(dict(name=name,transport=transport,tracking_epochs=sorted({r['tracking_epoch'] for r in data})))
        if name==LATEST[-1]:latest=data
    x=np.array([r['features'] for r in latest],dtype=np.float32);actions=np.array([r['action'][0] for r in latest])
    group_ids={'lateral':[4],'heading':[5,6],'lateral_rate':[24],'heading_rate':[25],
               'geometry':[7,8,9,10,11,12,26,27],'speed_profile':[1,2,28,29,30,31,32,33,34],'speed':[0]}
    jumps=[]
    for i in sorted(np.argsort(abs(np.diff(actions)))[-8:]+1):
        probes=np.repeat(x[i:i+1],len(group_ids),axis=0)
        for j,g in enumerate(group_ids.values()):probes[j,g]=x[i-1,g]
        predicted=predict(NEW,probes)[:,0]
        jumps.append(dict(t=latest[i]['t'],signed_s=latest[i]['s'],previous_action=float(actions[i-1]),action=float(actions[i]),
                          hold_previous_group_predictions=dict(zip(group_ids,map(float,predicted)))))
    seam=[]
    for a,b in zip(latest,latest[1:]):
        if not -5<=b['s']<=5:continue
        dx,dz=np.array(b['position'])-a['position'];h=a['reference_heading']
        seam.append(dict(t=b['t'],signed_s=b['s'],lateral_feature_change=b['features'][4]-a['features'][4],
            actual_displacement_m=math.hypot(dx,dz),lateral_displacement_fixed_smoothed_heading_m=math.cos(h)*dz-math.sin(h)*dx,
            lateral_rate=b['features'][24],reference_speed=b['features'][1]))
    sensitivities={}
    for label,model in [('old',OLD),('new',NEW)]:
        verify_hashes(read(model/'manifest.json')['output_sha256'],model);inputs.extend([model/'model.json',model/'manifest.json'])
        sensitivities[label]={}
        region=np.array([r['s']>=-132 for r in latest]);xx=x[region]
        for idx,step in [(4,.1),(24,1.),(25,.05)]:
            plus=xx.copy();minus=xx.copy();plus[:,idx]+=step;minus[:,idx]-=step
            d=abs(predict(model,plus)[:,0]-predict(model,minus)[:,0])
            sensitivities[label][str(idx)]=dict(perturbation=step,mean_action_span=float(d.mean()),p95_action_span=float(np.quantile(d,.95)))
    report=dict(runs=checks,large_policy_changes=jumps,seam_motion_check=seam,sensitivities=sensitivities,
        findings=['Large steering reversals begin before the projection seam, while model control remains active.',
                  'Position projection and its lateral derivative retain a finish-line discontinuity although heading/curvature are bridged.',
                  'The legacy reference-speed feature drops sharply at the finish line; it is not a runtime speed limit.',
                  'Frozen-feature diagnostics implicate response-rate features before the line and reference-speed/lateral changes at the seam.',
                  'The old model is also sensitive on the same failed observations; increased gain caused by augmentation is not established.'],
        limits=['Feature replacements are diagnostic hybrid observations, not closed-loop tests.',
                'Successful lap finishes and a hotlap first approach need not have identical unobserved tyre/vehicle state.',
                'This does not isolate which training component caused the regression.'])
    out=new_folder('reinforcement_runs','finish-line-diagnosis');save(out/'report.json',report);save(out/'series.json',series)
    finish(out,dict(kind='finish_line_diagnosis',input_sha256=hashes(inputs)))
    print('REPORT',out,flush=True)


def plot(folder):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    folder=Path(folder);series=read(folder/'series.json')
    plt.rcParams.update({'font.family':'Malgun Gothic','axes.unicode_minus':False})
    fig,axes=plt.subplots(2,2,figsize=(12,8),sharex=True)
    labels=['새 모델 시험 1','새 모델 시험 2','기존 모델: 성공 1랩 끝','기존 모델: 성공 2랩 끝']
    colors=['#dd795c','#c52a43','#008567','#2e69bb']
    for i,case in enumerate(series):
        d=case['data'];s=[r['s'] for r in d]
        for ax,v in zip(axes.flat,[[r['action'][0] for r in d],[r['features'][4] for r in d],
                                 [r['features'][24] for r in d],[r['features'][25] for r in d]]):
            ax.plot(s,v,label=labels[i],c=colors[i],lw=1.5,alpha=.85)
    for ax,title in zip(axes.flat,['모델 조향 요청 (왼쪽 - / 오른쪽 +)','기준 경로 횡오차 (m)','횡오차 변화율 특징 (m/s)','방향 오차 변화율 특징 (rad/s)']):
        ax.set_title(title);ax.axvline(0,c='black',lw=.8);ax.axvspan(-15,15,color='gray',alpha=.1);ax.grid(alpha=.2);ax.set_xlim(-150,105)
    for ax in axes[1]:ax.set_xlabel('스타트/피니시 라인 기준 경로 위치 (m)')
    handles,legend=axes[0,0].get_legend_handles_labels();fig.legend(handles,legend,loc='lower center',ncol=2)
    fig.suptitle('위빙 비교: 모델 제어 구간만 사용 · 정지 제어 제외')
    fig.tight_layout(rect=(0,.07,1,.96));fig.savefig(folder/'finish-line-comparison.png',dpi=150);plt.close(fig)


if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser();parser.add_argument('--plot');args=parser.parse_args()
    plot(args.plot) if args.plot else main()
