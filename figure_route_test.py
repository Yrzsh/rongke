# -*- coding: utf-8 -*-
import json, sys, tempfile
from pathlib import Path
from unittest.mock import patch
import threading
ROOT=Path(__file__).resolve().parent;sys.path.insert(0,str(ROOT))
from rongke.web.server import Manager
N=0
def check(x,t):
 global N;N+=1;assert x,t;print(f'[PASS] {N} {t}')
def main():
 with tempfile.TemporaryDirectory() as td:
  root=Path(td);rid='probe';ws=root/rid;ws.mkdir();(ws/'fig.py').write_text('def main(): pass',encoding='utf-8');(ws/'fig.pdf').write_bytes(b'pdf')
  m=Manager.__new__(Manager);m.kernel=None;m._lock=threading.RLock();m._artifact_lock=threading.RLock();m._artifact_cache={}
  with patch('rongke.web.server.ws_dir',return_value=root), patch.object(Manager,'busy',return_value=''):
   with patch.object(Manager,'busy',return_value='running'):
    check(m.tavotto_handoff(rid,'fig.pdf')['status']==409,'运行中交接409')
    check(m.tavotto_reaudit(rid,'tavotto/edit/fig.pdf')['status']==409,'运行中复审409')
   check(m.tavotto_handoff(rid,'../secret.pdf')['status']==400,'穿越路径拒绝')
   check(m.tavotto_handoff(rid,'missing.pdf')['status']==400,'缺图拒绝')
   check(m.tavotto_handoff(rid,'fig.py')['status']==400,'脚本不是图件')
   check(m.tavotto_reaudit(rid,'fig.pdf')['status']==400,'原图不能冒充精修图')
   with patch('rongke.figure_handoff.diagnose',return_value={'available':True,'reason':''}):
    missing_contract=m.tavotto_handoff(rid,'fig.pdf')
    check(missing_contract['status']==400 and '契约' in missing_contract['error'],'缺审计契约拒绝且不是扩展名误拦')
   (ws/'figure-qa.json').write_text(json.dumps({'files':{'pdf':'fig.pdf'},'contract':{'figure_id':'fig'}}),encoding='utf-8')
   check(m._figure_contract_for(rid,'fig.pdf')=={'figure_id':'fig'},'报告相对PDF路径可绑定契约')
   with patch('rongke.figure_handoff.diagnose',return_value={'available':False,'reason':'missing'}):
    unavailable=m.tavotto_handoff(rid,'fig.pdf')
    check(unavailable['status']==503,'合法图件在契约齐备后如实报Tavotto不可用')
 print(f'figure-route: {N} assertions passed')
if __name__=='__main__':main()
