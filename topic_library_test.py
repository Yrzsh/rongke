# -*- coding: utf-8 -*-
import json,tempfile
from pathlib import Path
from rongke import topic as T
n=0
def ck(label,cond):
 global n
 print(('✔ ' if cond else '✘ ')+label)
 if not cond:raise AssertionError(label)
 n+=1
with tempfile.TemporaryDirectory(dir='runs/_regress') as d:
 root=Path(d)
 T.ensure_topic('a','旧课题','旧问题',root=root)
 (root/'a'/'EVIDENCE.md').write_text('保留证据',encoding='utf-8')
 t=T.read_topic('a',root=root);ck('旧课题关联默认为空',t['library']==[])
 t=T.update_topic('a','新课题','新问题',root=root)
 ck('编辑保存且保留其他字段',t['title']=='新课题' and t['question']=='新问题' and t['created'])
 ck('编辑不碰台账',(root/'a'/'EVIDENCE.md').read_text(encoding='utf-8')=='保留证据')
 T.set_library('a','kb001',True,root=root);T.set_library('a','kb001',True,root=root);T.set_library('a','kb002',True,root=root)
 ck('关联稳定去重',T.read_topic('a',root=root)['library']==['kb001','kb002'])
 raw=json.loads((root/'a'/'topic.json').read_text(encoding='utf-8'));ck('原子写回真实JSON',raw['library']==['kb001','kb002'])
 T.set_library('a','kb001',False,root=root);ck('解除只删关联',T.read_topic('a',root=root)['library']==['kb002'])
 try:T.set_library('a','kb001',False,root=root);raise AssertionError('重复解除未报错')
 except ValueError:ck('重复解除明确失败',True)
 try:T.set_library('../bad','kb001',True,root=root);raise AssertionError('越界未拒绝')
 except ValueError:ck('非法课题不可写',True)
 ck('课题状态提供详情',T.status_view('a',root=root)['topic_details'][0]['library']==['kb002'])
print(f'全部通过 ✔（{n}项）')
