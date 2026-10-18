#!/usr/bin/env python3
"""Version 2: render saved values, not models or newly estimated metrics."""
import os
for key in ('OMP_NUM_THREADS','OPENBLAS_NUM_THREADS','MKL_NUM_THREADS','BLIS_NUM_THREADS','NUMEXPR_NUM_THREADS','VECLIB_MAXIMUM_THREADS'):
    os.environ[key] = '1'
import argparse, csv, hashlib, json, platform
from pathlib import Path
try:
    import resource
    resource.setrlimit(resource.RLIMIT_CPU, (120,120))
    resource.setrlimit(resource.RLIMIT_AS, (2*1024**3,2*1024**3))
except (ImportError, ValueError):
    pass
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch
ROOT = Path(__file__).resolve().parent
p = argparse.ArgumentParser(description=__doc__)
p.add_argument('--data', type=Path, default=ROOT/'data')
p.add_argument('--output', type=Path, default=ROOT/'rendered_landscape_v2')
a = p.parse_args()
if a.output.exists() and any(a.output.iterdir()):
    raise SystemExit('Refusing nonempty output: use a fresh version directory.')
a.output.mkdir(parents=True, exist_ok=True)
def sha(path): return hashlib.sha256(path.read_bytes()).hexdigest()
def table(name):
    with (a.data/name).open(newline='', encoding='utf-8') as f:
        return list(csv.DictReader(f))
expected={'overall_metrics.csv':'142eee1a9f9fb03bfcba725701240e4d1a5dd7c26890a93648351eaad2e058e5','monthly_metrics.csv':'b51b8b45f6a5ad05216ed0e3649a28462bfe575fed732ec6978b1e277d83c3da','cohorts.csv':'29af00b6601d5c58db600d6bf9bd9f25e653227bb4c1493c568011917fa587f4','periods.csv':'7010e2c205da8475ac3e55ddffa2c4f4493478578f9786382c20067801fa175d','recorded_results.json':'ce0098d5c7fc569b4bf85199bccc7c2b14e0ef27b557b550ec5a9b1ddd6533c4'}
checks=[]
def check(ok, message):
    if not ok: raise AssertionError(message)
    checks.append(message)
check(all(sha(a.data/n)==v for n,v in expected.items()), 'All five data files match supplied hashes')
r=json.loads((a.data/'recorded_results.json').read_text())
overall=table('overall_metrics.csv'); monthly=table('monthly_metrics.csv'); cohorts=table('cohorts.csv'); periods=table('periods.csv')
for name,rows,keys in [('overall_metrics',overall,('model','policy')),('monthly_metrics',monthly,('month','model','policy')),('cohorts',cohorts,('month',))]:
    check(len(rows)==len(r[name]), name+' CSV/JSON counts')
    for row in rows:
        matches=[t for t in r[name] if all(str(t[k])==row[k] for k in keys)]
        check(len(matches)==1, name+' unique record '+str(tuple(row[k] for k in keys)))
        for k,v in row.items():
            check(float(v)==matches[0][k] if isinstance(matches[0][k],(int,float)) else v==matches[0][k], name+' exact '+k)
O={(t['model'],t['policy']):t for t in overall}
check(len(overall)==4 and len(O)==4, 'Four model/policy records')
for c in cohorts: check(c['fit_last'][:7]<c['month'], 'Strictly earlier history '+c['month'])
for key,total in [('eligible',3493),('available',3711),('excluded',218)]:
    check(sum(int(t[key]) for t in cohorts)==total, 'Cohort total '+key)
for model in ('linear','quadratic'):
    for metric in ('mae','rmse'):
        vals=[float(t[metric]) for t in monthly if t['model']==model and t['month']=='2004-11']
        check(len(vals)==2 and vals[0]==vals[1], 'November equality '+model+' '+metric)
pr={t['period']:t for t in periods}
for key,n in [('train',4053),('validation',1445),('refit',5498),('test',3493)]: check(int(pr[key]['eligible'])==n, 'Period '+key)
plt.rcParams.update({'font.family':'DejaVu Sans','font.size':7,'svg.fonttype':'none','svg.hashsalt':'benzene-v2','text.color':'black','savefig.bbox':None})
fig=plt.figure(figsize=(7,4.2), dpi=100, facecolor='white')
ax=fig.add_axes([0,0,1,1]); ax.set_xlim(0,504); ax.set_ylim(302.4,0); ax.axis('off')
def text(x,y,s,size=7,weight='normal',ha='left'):
    return ax.text(x,y,s,fontsize=size,weight=weight,ha=ha,va='top',linespacing=1.2)
def box(x,y,w,h,fill='1',dashed=False):
    ax.add_patch(FancyBboxPatch((x,y),w,h,boxstyle='round,pad=0,rounding_size=2',facecolor=fill,edgecolor='0.4',lw=.65,linestyle='--' if dashed else '-'))
text(8,7,'Benzene calibration replay',11,'bold')
text(8,24,'Frozen versus monthly-updated Ridge • saved results only',8)
box(8,40,236,66,'0.96')
text(14,46,'A  HISTORICAL FITTING',8,'bold')
text(14,61,'Inputs + C6H6(GT) reference labels\nTrain: Mar–Aug 2004; n = 4,053\nValidate: Sep–Oct; n = 1,445\nRefit through 31 Oct; n = 5,498')
text(166,61,'8 inputs / 44 terms\nLinear / quadratic\nScale → Ridge\nα = 0.1; fixed later')
box(8,114,236,74,'1')
text(14,120,'B  PREDICTION-TIME INPUTS',8,'bold')
text(14,135,'Five sensors + temperature, RH, AH → fitted model\nFrozen: coefficients fixed after 31 Oct 2004\nMonthly: expanding refits; labels strictly earlier\nthan each month boundary. No current-month\nreference label is a prediction input.')
box(8,196,236,52,'0.96',True)
text(14,202,'C  EVALUATION LABELS / SCORE ONLY',8,'bold')
text(14,217,'C6H6(GT) scores the same 3,493 eligible hours\n1 Nov 2004–4 Apr 2005, 14:00. Earlier evaluation\nlabels enter later refits: not a label-isolated test.')
box(254,40,242,208,'1',True)
text(261,46,'D  OVERALL ERRORS',8,'bold')
text(261,60,'n = 3,493 • MAE and RMSE in µg/m³ • lower is better')
# Legend uses both fill and shape for grayscale discrimination.
ax.plot(266,79,'o',ms=4.3,mfc='white',mec='black',mew=.9)
text(274,74,'Frozen')
ax.plot(323,79,'s',ms=4.1,mfc='black',mec='black',mew=.8)
text(331,74,'Monthly')
text(489,74,'Separate linear scales',7,ha='right')
displays=[]
# Each family's two metrics share a zero-based horizontal linear scale.
# Scales differ between families, with explicit maxima and labeled ticks.
x0,x1=302,482
for model,heading,ys,axis_y,maximum,ticks in [
    ('linear',89,(113,136),148,3.0,(0,1,2,3)),
    ('quadratic',171,(194,217),229,0.18,(0,.06,.12,.18))]:
    text(261,heading,model.title()+' Ridge',7.5,'bold')
    text(489,heading,('0–3' if model=='linear' else '0–0.18')+' µg/m³',7,ha='right')
    def xpos(v): return x0+(x1-x0)*v/maximum
    for tick in ticks:
        x=xpos(tick)
        ax.plot([x,x],[ys[0]-3,axis_y],color='0.89',lw=.5,zorder=0)
        ax.plot([x,x],[axis_y,axis_y+2],color='0.3',lw=.6)
        label=f'{tick:g}' if model=='linear' else ('0' if tick==0 else f'{tick:.2f}')
        text(x,axis_y+3,label,7,ha='center')
    ax.plot([x0,x1],[axis_y,axis_y],color='0.4',lw=.65)
    for metric,y in zip(('mae','rmse'),ys):
        text(261,y-4,metric.upper(),7)
        frozen=float(O[model,'frozen'][metric]); updated=float(O[model,'updated'][metric])
        ax.plot([xpos(updated),xpos(frozen)],[y,y],color='0.3',lw=1.4,zorder=2)
        for policy,v,marker,fill in [('frozen',frozen,'o','white'),('updated',updated,'s','black')]:
            ax.plot(xpos(v),y,marker,ms=4.3,mfc=fill,mec='black',mew=.8,zorder=3)
            value=f'{v:.4f}'
            text(xpos(v),y-13,value,7,ha='center')
            saved=next(t for t in r['overall_metrics'] if t['model']==model and t['policy']==policy)
            check(value==f'{saved[metric]:.4f}', 'Exact recorded four-decimal display '+model+' '+policy+' '+metric)
            displays.append({'model':model,'policy':policy,'metric':metric,'recorded':O[model,policy][metric],'display':value,'x_points':xpos(v),'y_points':y,'axis_max':maximum})
box(8,256,488,39,'0.94')
text(14,261,'EVIDENCE LIMITS',7.5,'bold')
text(14,274,'One archive; dependent hours. Complete cases: 218 / 3,711 excluded (−200 / blanks; no imputation). November identical.\nNo external-site test, drift causality or target independence established. Label operations, QC, rollback and deployment untested.',7)
fig.canvas.draw(); renderer=fig.canvas.get_renderer()
texts=list(ax.texts); bounds=[t.get_window_extent(renderer) for t in texts]
check(all(b.x0>=0 and b.y0>=0 and b.x1<=fig.bbox.width and b.y1<=fig.bbox.height for b in bounds), 'All text inside native page')
collisions=[]
for i,b in enumerate(bounds):
    for j,c in enumerate(bounds[:i]):
        if min(b.x1,c.x1)>max(b.x0,c.x0) and min(b.y1,c.y1)>max(b.y0,c.y0):
            collisions.append([texts[j].get_text(),texts[i].get_text()])
check(not collisions, 'Text boxes pairwise nonoverlapping: '+str(collisions))
for fmt in ('svg','png'):
    fig.savefig(a.output/('main.'+fmt),**({'dpi':600} if fmt=='png' else {}))
fig.savefig(a.output/'preview.png',dpi=200)
# Review crop is saved directly from the same canvas at high resolution.
from matplotlib.transforms import Bbox
fig.savefig(a.output/'panel_D_review.png',dpi=300,bbox_inches=Bbox.from_extents(252/72,(302.4-250)/72,498/72,(302.4-38)/72))
plt.close(fig)
audit={'checks_passed':checks,'native_size_mm':[177.8,106.68],'minimum_font_pt':7,'scales':{'linear':[0,3],'quadratic':[0,.18],'type':'linear; common within each family, not between families'},'overall_display':displays,'source_files':{n:sha(a.data/n) for n in expected},'text_geometry':[{'text':t.get_text(),'bbox_points':[v*72/fig.dpi for v in (b.x0,b.y0,b.x1,b.y1)]} for t,b in zip(texts,bounds)],'scope':'Saved-value and geometry checks; not scientific or visual certification. Separate visual inspection required.','python':platform.python_version(),'matplotlib':matplotlib.__version__,'outputs':{n:{'sha256':sha(a.output/n),'bytes':(a.output/n).stat().st_size} for n in ('main.svg','main.png','preview.png','panel_D_review.png')}}
(a.output/'audit.json').write_text(json.dumps(audit,indent=2)+'\n')
print(json.dumps({'checks_passed':len(checks),'outputs':audit['outputs']},indent=2))
