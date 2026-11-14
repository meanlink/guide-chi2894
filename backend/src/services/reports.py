import time, os, csv, io, hashlib, json, logging
from sqlalchemy import select
from fastapi import HTTPException
from src.models import Job, Report, Delivery
from src.services.audit import record
from src.config.settings import ARTIFACT_DIR
RETRY_DELAY_SECONDS = 60
def create(db, data):
    j=db.get(Job,data.job_id)
    if not j or j.status!='completed' or not j.approved:
        raise HTTPException(409,'请先选择已完成且获批准的分析结果')
    r=Report(name=data.name, job_id=j.id, destination=data.destination, interval_seconds=data.interval_seconds, next_run=time.time()+data.delay_seconds, enabled=True)
    db.add(r); db.flush()
    return r
def _issue_filename(report_id, scheduled_for):
    # 同一计划的同一期始终得到同一文件名，重跑不会产生第二份归档
    return f'report-{report_id}-issue-{int(round(scheduled_for*1000))}.csv'
def _render_csv(result):
    out=io.StringIO()
    out.write('\ufeff')
    table=result.get('table',{})
    cols=table.get('columns',[])
    writer=csv.writer(out)
    def safe(v):
        if isinstance(v,(int,float)) and not isinstance(v,bool): return v
        text=str(v)
        return "'"+text if text.startswith(('=','+','-','@','\t','\r')) else text
    writer.writerow([safe(c['label']) for c in cols])
    for row in table.get('rows',[]): writer.writerow([safe(row.get(c['key'],'')) for c in cols])
    return out.getvalue().encode('utf-8')
def _render_json(snapshot, checksum):
    return json.dumps({'job':snapshot.get('job'),'params':snapshot.get('params'),'result':snapshot.get('result'),'checksum':checksum},ensure_ascii=False,allow_nan=False).encode('utf-8')
def _write_atomic(path, data):
    # 先写临时文件再原子改名，中断不会在校验路径上留下半份文件
    tmp=path+'.tmp'
    with open(tmp,'wb') as f:
        f.write(data); f.flush(); os.fsync(f.fileno())
    os.replace(tmp,path)
def _snapshot_of(job):
    return {'job_id':job.id,'job':job.name,'params':job.params,'result':job.result}
def _mark_failed(db, report_id, scheduled_for, delivery_id, destination, error):
    # 失败原因持久化到同一期次行，收件箱可查；排期稍后重试，不影响其他到期计划
    try:
        d=db.get(Delivery,delivery_id) if delivery_id else None
        if d is None:
            d=db.scalar(select(Delivery).where(Delivery.report_id==report_id,Delivery.scheduled_for==scheduled_for))
        if d is None:
            d=Delivery(report_id=report_id,created_at=time.time(),checksum='',filename='',destination=destination,scheduled_for=scheduled_for,status='failed')
            db.add(d)
        d.status='failed'; d.error=(error or '未知错误')[:990]
        r=db.get(Report,report_id)
        if r: r.next_run=time.time()+RETRY_DELAY_SECONDS
        record(db,'worker','report.delivery_failed',{'id':report_id,'error':d.error})
        db.commit()
    except Exception:
        db.rollback()
        logging.exception('Failed to record delivery failure')
def deliver(db, report):
    report_id=report.id
    scheduled_for=report.next_run
    destination=report.destination
    interval_seconds=report.interval_seconds
    delivery=db.scalar(select(Delivery).where(Delivery.report_id==report_id,Delivery.scheduled_for==scheduled_for))
    if delivery is not None and delivery.status=='archived':
        # 上一期已归档但排期未推进（归档后崩溃），只补推进，不再归档第二份
        report.next_run=time.time()+interval_seconds
        db.commit()
        return None
    if delivery is None:
        delivery=db.scalar(select(Delivery).where(Delivery.report_id==report_id,Delivery.status.in_(('pending','failed'))).order_by(Delivery.id).limit(1))
    delivery_id=delivery.id if delivery is not None else None
    try:
        if delivery is None:
            j=db.get(Job,report.job_id)
            if not j or not j.approved: raise RuntimeError('关联分析不存在或尚未获批准，无法生成简报')
            delivery=Delivery(report_id=report_id,created_at=time.time(),checksum='',filename=_issue_filename(report_id,scheduled_for),destination=destination,status='pending',scheduled_for=scheduled_for,snapshot=_snapshot_of(j))
            db.add(delivery)
            db.commit()
            delivery_id=delivery.id
        if not delivery.snapshot:
            # 首次尝试未取到批准结果的期次，续做时补取当前批准结果
            j=db.get(Job,report.job_id)
            if not j or not j.approved: raise RuntimeError('关联分析不存在或尚未获批准，无法生成简报')
            delivery.snapshot=_snapshot_of(j)
        if not delivery.filename:
            delivery.filename=_issue_filename(report_id,delivery.scheduled_for)
        db.commit()
        # 续做沿用该期首次取到的批准结果快照，此后的分析变化不影响本期
        csv_bytes=_render_csv(delivery.snapshot.get('result') or {})
        checksum=hashlib.sha256(csv_bytes).hexdigest()
        json_bytes=_render_json(delivery.snapshot,checksum)
        os.makedirs(ARTIFACT_DIR,exist_ok=True)
        base=os.path.join(ARTIFACT_DIR,delivery.filename)
        _write_atomic(base,csv_bytes)
        _write_atomic(base+'.json',json_bytes)
        delivery.checksum=checksum
        delivery.checksum_json=hashlib.sha256(json_bytes).hexdigest()
        delivery.status='archived'
        delivery.error=''
        report.next_run=time.time()+interval_seconds
        db.commit()
        return delivery
    except Exception as e:
        logging.exception('Delivery failed')
        db.rollback()
        _mark_failed(db,report_id,scheduled_for,delivery_id,destination,str(e))
        return None
