"""Prospective serving-metadata policy; no credentials or completion retries."""
import json,math,time
from datetime import datetime,timezone
from email.utils import parsedate_to_datetime
from decimal import Decimal,InvalidOperation

class ReadinessStopped(RuntimeError):
    def __init__(self):super().__init__('Selected model readiness unverified; no call reserved')

def admitted(data,model):
    if not isinstance(data,dict) or not isinstance(data.get('data'),dict):return False
    d=data['data']
    if d.get('id')!=model or not isinstance(d.get('endpoints'),list) or not d['endpoints']:return False
    for e in d['endpoints']:
        if not isinstance(e,dict) or e.get('model_id')!=model:return False
        # Conservative POLICY predicate based on official status0 example, not a documented enum guarantee.
        if type(e.get('status')) is not int or e['status']!=0:continue
        prices=e.get('pricing')
        if not isinstance(prices,dict):continue
        try:
            if any(type(prices.get(k)) not in (str,int,float) or not Decimal(str(prices[k])).is_finite() or Decimal(str(prices[k]))!=0 for k in ('prompt','completion')):continue
            if any(type(v) not in (str,int,float) or not Decimal(str(v)).is_finite() or Decimal(str(v))!=0 for k,v in prices.items() if k in ('request','image','web_search','internal_reasoning')):continue
        except (InvalidOperation,ValueError,KeyError):continue
        for k in ('uptime_last_5m','uptime_last_30m'):
            v=e.get(k)
            if v is not None and (type(v) not in (int,float) or not math.isfinite(v) or not 0<v<=100):break
        else:return True
    return False

def check(get,base,model,now=None,clock=None):
    now=now or (lambda:datetime.now(timezone.utc));clock=clock or time.monotonic
    start=clock();response=None
    try:
        response=get(base+'/models/'+model+'/endpoints',timeout=(5,5),allow_redirects=False,stream=True)
        if response.status_code!=200:raise ReadinessStopped()
        date=parsedate_to_datetime(response.headers['Date'])
        if date.tzinfo is None:raise ReadinessStopped()
        age=(now()-date).total_seconds();cache_age=float(response.headers.get('Age','0'))
        if not math.isfinite(cache_age) or not 0<=cache_age<=60 or not -5<=age<=60:raise ReadinessStopped()
        body=bytearray()
        for chunk in response.iter_content(chunk_size=8192):
            body.extend(chunk)
            if len(body)>524288 or clock()-start>10:raise ReadinessStopped()
        data=json.loads(body)
        if clock()-start>10 or (now()-date).total_seconds()>60 or not admitted(data,model):raise ReadinessStopped()
    except Exception:
        raise ReadinessStopped() from None
    finally:
        if response is not None:
            try:response.close()
            except Exception:pass
