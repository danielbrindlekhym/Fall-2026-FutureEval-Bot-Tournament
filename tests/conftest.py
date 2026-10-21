"""Offline route metadata fixture for inherited API/receipt regressions.
Uses real readiness parser; prevents all live networking. Pool cases tested separately.
"""
import json,socket
from datetime import datetime,timezone
from email.utils import format_datetime
import pytest
import futureeval_bot.api as api
from futureeval_bot.readiness import check
@pytest.fixture(autouse=True)
def offline_metadata(monkeypatch):
 original_connect=socket.socket.connect
 def no_network(sock,address,*a,**kw):
  if isinstance(address,tuple) and address[0] in ('127.0.0.1','::1'):return original_connect(sock,address,*a,**kw)
  raise AssertionError('External networking forbidden in offline release checks')
 monkeypatch.setattr(socket.socket,'connect',no_network)
 def fixture_get(url,**kw):
  model=url.split('/models/',1)[1].rsplit('/endpoints',1)[0]
  class Response:
   status_code=200
   headers={'Date':format_datetime(datetime.now(timezone.utc))}
   def iter_content(self,**kw):yield json.dumps({'data':{'id':model,'endpoints':[{'model_id':model,'status':0,'pricing':{'prompt':'0','completion':'0'}}]}}).encode()
   def close(self):pass
  return Response()
 monkeypatch.setattr(api,'check_readiness',lambda get,base,model:check(fixture_get,base,model))
