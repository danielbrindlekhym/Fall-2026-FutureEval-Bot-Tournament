"""Prospective serving-metadata policy; no credentials or completion retries."""
import json,math,time
import requests
from datetime import datetime,timezone
from email.utils import parsedate_to_datetime
from decimal import Decimal,InvalidOperation

CHECK_FAILURE_CODES = frozenset({
    "http_status_non_200", "date_header_invalid", "cache_age_invalid",
    "response_age_out_of_bounds", "response_body_limit", "response_read_time_limit",
    "json_invalid", "metadata_not_admitted", "transport_exception", "unexpected_exception",
})
SELECTOR_STOP_CODES = frozenset({
    "prior_stopped_attempt", "insufficient_readiness_time",
    "no_candidate_admitted", "no_candidate_selected", "unknown_selector_stop",
})

class ReadinessStopped(RuntimeError):
    def __init__(self, check_failure_counts=None, selector_stop=None):
        super().__init__("Selected model readiness unverified; no call reserved")
        safe = {}
        if isinstance(check_failure_counts, dict):
            for code, count in check_failure_counts.items():
                target = code if isinstance(code, str) and code in CHECK_FAILURE_CODES else "unexpected_exception"
                if type(count) is int and count > 0:
                    safe[target] = safe.get(target, 0) + count
        self.diagnostics = {
            "check_failure_counts": safe,
            "selector_stop": selector_stop if isinstance(selector_stop, str) and selector_stop in SELECTOR_STOP_CODES else
                ("unknown_selector_stop" if selector_stop is not None else None),
        }

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
        try:
            response=get(base+'/models/'+model+'/endpoints',timeout=(5,5),allow_redirects=False,stream=True)
        except requests.RequestException:
            raise ReadinessStopped({"transport_exception": 1}) from None
        if response.status_code!=200:
            raise ReadinessStopped({"http_status_non_200": 1})
        try:
            date=parsedate_to_datetime(response.headers['Date'])
            if date.tzinfo is None:
                raise ValueError("invalid date")
        except Exception:
            raise ReadinessStopped({"date_header_invalid": 1}) from None
        try:
            age=(now()-date).total_seconds()
        except Exception:
            raise ReadinessStopped({"unexpected_exception": 1}) from None
        try:
            cache_age=float(response.headers.get('Age','0'))
        except Exception:
            raise ReadinessStopped({"cache_age_invalid": 1}) from None
        if not math.isfinite(cache_age) or not 0<=cache_age<=60:
            raise ReadinessStopped({"cache_age_invalid": 1})
        if not -5<=age<=60:
            raise ReadinessStopped({"response_age_out_of_bounds": 1})
        body=bytearray()
        try:
            for chunk in response.iter_content(chunk_size=8192):
                body.extend(chunk)
                if len(body)>524288:
                    raise ReadinessStopped({"response_body_limit": 1})
                if clock()-start>10:
                    raise ReadinessStopped({"response_read_time_limit": 1})
        except ReadinessStopped:
            raise
        except requests.RequestException:
            raise ReadinessStopped({"transport_exception": 1}) from None
        except Exception:
            raise ReadinessStopped({"unexpected_exception": 1}) from None
        try:
            data=json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError, TypeError):
            raise ReadinessStopped({"json_invalid": 1}) from None
        if clock()-start>10:
            raise ReadinessStopped({"response_read_time_limit": 1})
        if (now()-date).total_seconds()>60:
            raise ReadinessStopped({"response_age_out_of_bounds": 1})
        try:
            accepted=admitted(data,model)
        except Exception:
            raise ReadinessStopped({"unexpected_exception": 1}) from None
        if not accepted:
            raise ReadinessStopped({"metadata_not_admitted": 1})
    except ReadinessStopped:
        raise
    except requests.RequestException:
        raise ReadinessStopped({"transport_exception": 1}) from None
    except Exception:
        raise ReadinessStopped({"unexpected_exception": 1}) from None
    finally:
        if response is not None:
            try:response.close()
            except Exception:pass
