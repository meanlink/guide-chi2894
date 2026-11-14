import os, json, hashlib
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy import select
from src.config.database import get_db
from src.config.settings import ARTIFACT_DIR
from src.models import Report, Delivery
from src.schemas.report import ReportInput
from src.middlewares.auth import current_user, admin_only
from src.services import reports
from src.services.audit import record
from src.utils.serialization import report_dict, delivery_dict
router=APIRouter()
@router.get('/reports')
def listing(db=Depends(get_db),u=Depends(current_user)):
    return [report_dict(r) for r in db.scalars(select(Report).order_by(Report.id.desc()))]
@router.post('/reports',status_code=201)
def create(data:ReportInput,db=Depends(get_db),u=Depends(admin_only)):
    r=reports.create(db,data); record(db,u.email,'report.schedule',{'id':r.id,'destination':r.destination}); db.commit()
    return report_dict(r)
@router.post('/reports/{id}/toggle')
def toggle(id:int,db=Depends(get_db),u=Depends(admin_only)):
    r=db.get(Report,id)
    if not r:raise HTTPException(404,'分发计划不存在')
    r.enabled=not r.enabled;record(db,u.email,'report.toggle',{'id':id,'enabled':r.enabled});db.commit()
    return report_dict(r)
@router.get('/deliveries')
def deliveries(db=Depends(get_db),u=Depends(current_user)):
    return [delivery_dict(d) for d in db.scalars(select(Delivery).order_by(Delivery.id.desc()).limit(100))]
def _verify_pair(d):
    # 表格与说明文件都完整且校验一致，才允许下载任一份
    base=os.path.join(ARTIFACT_DIR,d.filename)
    jp=base+'.json'
    if not d.filename or not os.path.exists(base) or not os.path.exists(jp):
        raise HTTPException(404,'归档文件缺失，请等待重新分发')
    with open(base,'rb') as f:
        if hashlib.sha256(f.read()).hexdigest()!=d.checksum:
            raise HTTPException(409,'表格文件校验不一致，归档已损坏，请等待重新分发')
    try:
        with open(jp,encoding='utf-8') as f: meta=json.load(f)
    except Exception:
        raise HTTPException(409,'说明文件无法解析，归档已损坏，请等待重新分发')
    if meta.get('checksum')!=d.checksum:
        raise HTTPException(409,'说明文件与表格校验不一致，归档已损坏，请等待重新分发')
    if d.checksum_json:
        with open(jp,'rb') as f:
            if hashlib.sha256(f.read()).hexdigest()!=d.checksum_json:
                raise HTTPException(409,'说明文件校验不一致，归档已损坏，请等待重新分发')
    return base,jp
@router.get('/deliveries/{id}/download')
def download(id:int,format:str='csv',db=Depends(get_db),u=Depends(current_user)):
    d=db.get(Delivery,id)
    if not d:raise HTTPException(404,'归档不存在')
    if format not in ('csv','json'):raise HTTPException(422,'支持 CSV 或 JSON')
    status=d.status or 'archived'
    if status=='failed':raise HTTPException(409,'该期简报归档失败：'+(d.error or '未知错误'))
    if status!='archived':raise HTTPException(409,'该期简报正在生成中，请稍后重试')
    base,jp=_verify_pair(d)
    if format=='json':return FileResponse(jp,filename=d.filename+'.json',media_type='application/json')
    return FileResponse(base,filename=d.filename,media_type='text/csv; charset=utf-8')
