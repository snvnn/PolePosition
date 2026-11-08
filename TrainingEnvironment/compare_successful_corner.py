"""Compare actual autonomous passes at matched path positions, never stop control."""
import json
from pathlib import Path
import numpy as np
from ModelTrainer.common import read, verify_hashes, new_folder

ARCHIVE=Path('reinforcement_runs/autonomous-two-laps-1793422357204916300-c70c929b')
FAILURES=['gamepad-1793419197708887500-32076','gamepad-1793419408994112000-11348','gamepad-1793419625640624800-19680']


def load_passes():
    success=read(ARCHIVE/'successful-laps.json');verify_hashes(success['evidence_sha256'])
    specs=[(f'failure_{i+1}',Path('actuator_runs')/n,None,None) for i,n in enumerate(FAILURES)]
    for lap in success['successful_full_laps']:
        specs.append((f'success_lap_{lap["lap"]}',ARCHIVE/'source-run',lap['start_observation_ns'],lap['end_observation_ns']))
    specs.append(('same_session_failed_lap_3',ARCHIVE/'source-run',success['successful_full_laps'][-1]['end_observation_ns'],None))
    cache={};passes=[];settings=None
    for name,folder,start,end in specs:
        if str(folder) not in cache:
            m=read(folder/'manifest.json')
            # The archived files are checked against the snapshot's evidence inventory.
            if folder!=ARCHIVE/'source-run':verify_hashes(m['output_sha256'],folder)
            if m['model_sha256']!=success['model_sha256'] or m['steering_smoothing_ms']!=0:raise ValueError('Model/settings differ')
            signature={k:m[k] for k in ('controls_actual_sha256','profile_sha256','disable_path_departure_stop','seconds')}
            if settings is not None and signature!=settings:raise ValueError('Execution settings differ')
            settings=signature
            rows=[json.loads(s) for s in (folder/'samples.jsonl').read_text().splitlines()]
            cache[str(folder)]=(m,rows)
        m,rows=cache[str(folder)]
        obs={r['observation_ns']:r for r in rows if r['kind']=='observation'}
        data=[]
        for p in rows:
            if p['kind']!='policy' or p['mode']!='Running':continue
            t=p['observation_ns']
            if (start is not None and t<start) or (end is not None and t>=end):continue
            o=obs[t];s=o['state']['s_m'];state=o['state'];sample=o['sample']
            if s is None or not 450<=s<=1000:continue
            if p['authority']!='model' or any(p['label_mask']):raise ValueError('Human intervention in comparison interval')
            data.append(dict(t=t/1e9,s=s,speed=sample['speed_kmh'],lateral=state['lateral_error_m'],heading=state['heading_error_rad'],
                stick=p['selected_action'][0],rt=p['selected_action'][1],lt=p['selected_action'][2],acc_steer=sample['steering_raw'],
                acc_rt=sample['throttle'],acc_lt=sample['brake'],valid=sample['lap_valid'],lateral_rate=p['features'][24],heading_rate=p['features'][25]))
        passes.append(dict(name=name,folder=str(folder),data=data))
    return passes


def sustained(data,key,threshold,lower,upper,duration=.15,less=True):
    first=None;previous=None
    for r in data:
        ok=lower<=r['s']<=upper and (r[key]<threshold if less else r[key]>threshold)
        if not ok or (previous is not None and r['t']-previous>.05):first=None
        if ok:
            if first is None:first=r
            if r['t']-first['t']>=duration:return {k:first[k] for k in ('s','t','speed','lateral','heading',key)}
        previous=r['t']
    return None


def plot(passes,out):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    plt.rcParams.update({'font.family':'Malgun Gothic','axes.unicode_minus':False,'font.size':10})
    fig,axes=plt.subplots(3,2,figsize=(13,11),sharex=True)
    labels=['이전 실패 1','이전 실패 2','이전 실패 3','성공 랩 1','성공 랩 2','같은 세션: 3랩 실패']
    colors=['#c58a8a','#dd7866','#cc344c','#007f67','#2a67cc','#942ab5']
    fields=[('stick','모델 스틱 요청 (왼쪽 - / 오른쪽 +)'),('acc_steer','ACC 조향 관측값 (원시 단위)'),
            ('lateral','기준 경로 대비 횡방향 위치 (m)'),('heading','기준 경로 대비 방향 오차 (rad)'),
            ('speed','속도 (km/h)'),('acc_lt','ACC 브레이크 관측값 (0~1)')]
    for ax,(key,title) in zip(axes.flat,fields):
        for i,p in enumerate(passes):
            d=p['data']
            ax.plot([r['s'] for r in d],[r[key] for r in d],label=labels[i],color=colors[i],
                    lw=2 if i in (3,4) else 1.25,ls='--' if i==5 else '-',alpha=.7 if i<2 else 1)
        ax.set_title(title,loc='left');ax.grid(alpha=.2);ax.set_xlim(650,950)
        ax.axvspan(790,850,color='#888888',alpha=.06)
        for x in (770,810,850):ax.axvline(x,color='#999999',lw=.6,ls=':')
    for ax in axes[-1]:ax.set_xlabel('출발선 기준 경로 위치 (m)')
    fig.suptitle('같은 모델·입력 설정·LPF 꺼짐: 성공 2랩과 실패 4회\n정책이 직접 제어한 구간만 비교 · 종료 후 제동/재시작 제외',fontsize=15)
    handles,legend=axes[0,0].get_legend_handles_labels()
    fig.legend(handles,legend,loc='lower center',ncol=3,bbox_to_anchor=(.5,.008))
    fig.tight_layout(rect=(0,.065,1,.935))
    fig.savefig(out/'corner-comparison.png',dpi=160)
    plt.close(fig)


def main():
    passes=load_passes();out=new_folder('reinforcement_runs','successful-corner-comparison')
    results=[]
    for p in passes:
        data=p['data'];metrics={}
        for name,args in {'stick_left':('stick',-.2,650,770),'acc_left':('acc_steer',-.03,650,790),'brake20':('lt',.2,740,850),'brake80':('lt',.8,740,880)}.items():
            metrics[name]=sustained(data,*args,less='brake' not in name)
        metrics['max_lateral_650_790']=max(abs(r['lateral']) for r in data if 650<=r['s']<=790)
        metrics['max_lateral_790_900']=max((abs(r['lateral']) for r in data if 790<=r['s']<=900),default=None)
        metrics['minimum_speed_750_950']=min(r['speed'] for r in data if 750<=r['s']<=950)
        metrics['maximum_acc_steer_800_850']=max((r['acc_steer'] for r in data if 800<=r['s']<=850),default=None)
        opposite=[r for r in data if 790<=r['s']<=850 and r['stick']<-.3 and r['acc_steer']>.03]
        metrics['opposite_acc_sign_790_850']=dict(count=len(opposite),first_s=opposite[0]['s'] if opposite else None,last_s=opposite[-1]['s'] if opposite else None)
        metrics['observed_end_s']=data[-1]['s']
        points=[]
        for pos in [500,550,600,650,670,690,710,730,750,770,790,810,830,850,880,920,950]:
            near=[r for r in data if abs(r['s']-pos)<=2]
            if near:points.append(dict(s=pos,count=len(near),**{k:float(np.mean([r[k] for r in near])) for k in ['speed','lateral','heading','stick','rt','lt','acc_steer','acc_lt','lateral_rate','heading_rate']}))
        results.append(dict(name=p['name'],metrics=metrics,points=points))
    result=dict(interval_m=[450,1000],runs=results,source='actual model-policy control only; stopping and reset excluded',
        position_comparison='means of actual samples within +/-2m; no extrapolation',
        crossing_definition='threshold sustained for 0.15 seconds with no observation gap >50ms',
        limitation='Two successful laps and four failures are observational evidence, not a controlled causal intervention.')
    (out/'comparison.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
    (out/'series.json').write_text(json.dumps(passes),encoding='utf-8')
    plot(passes,out)
    print('REPORT',out,flush=True)
    for r in results:
        print(r['name'],'samples',len(next(p['data'] for p in passes if p['name']==r['name'])),flush=True)

if __name__=='__main__':main()
