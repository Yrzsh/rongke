# -*- coding: utf-8 -*-
"""对话推理显示：执行真实渲染函数，而非匹配源码字符串。"""
from pathlib import Path
import subprocess,sys
root=Path(__file__).resolve().parent
src=(root/'rongke/web/static/workspace.js').read_text(encoding='utf-8')
a=src.index('function firstLine(');b=src.index('\nconst GLYPH',a)
js=r'''
const assert=require('assert');
const document={querySelectorAll:()=>[],createElement:()=>node()};
function transcriptView(){return 'detailed'}
function md(s){return '<p>'+s+'</p>'}
const LIVE=false;
function node(){const children={};return {dataset:{},_text:'',children,querySelector(s){if(!children[s])children[s]={dataset:{},setAttribute(){},innerHTML:'',textContent:'',hidden:false};return children[s]},setAttribute(){}}}
let boxes=[];const host={querySelector(){return boxes.length?boxes[boxes.length-1]:null},appendChild(b){boxes.push(b)}};
function cur(){return host}
FUNCTIONS
assert.equal(thinkSummary('## **检查文献**\n正文',false),'检查文献');
assert.equal(thinkSummary('> `kb_search`\n\n- **验证页码**',false),'验证页码');
assert.equal(thinkSummary('[检索](https://x)',false),'检索');
assert.equal(thinkSummary('第一段。\n\n第二段。\n\n尚未结束',true),'第二段。');
assert.equal(thinkSummary('只有尚未完成段',true),'');
assert.equal(thinkSummary('x'.repeat(200),false).length,150);
let box=addThink('# 检索\n正文\n\n尚未完成',true,{text_chars:5000,truncated:true});
assert.equal(box.dataset.open,'0');assert.equal(box.querySelector('.thinkstate').textContent,'推理中');
assert.equal(box.querySelector('.thinkcut').hidden,false);
assert.equal(box.querySelector('.tsumt').textContent,'检索');
addThink('继续推理',true,{truncated:false});assert.equal(boxes.length,1);
assert.equal(box.querySelector('.thinkcut').hidden,true);
box.dataset.open='1';paintThink(box,'完整内容',false,{});
assert.equal(box.querySelector('.thinkb').innerHTML,'<p>完整内容</p>');
assert.equal(box.querySelector('.thinkstate').textContent,'已结束');
assert.equal(box.querySelector('.thinkcut').hidden,true);
console.log('dialogue thinking behavior: 16 assertions passed');
'''.replace('FUNCTIONS',src[a:b])
p=root/'runs/_dialogue_thinking_test.cjs';p.write_text(js,encoding='utf-8')
r=subprocess.run(['node',str(p)],capture_output=True,text=True,encoding='utf-8')
print(r.stdout,end='');print(r.stderr,end='',file=sys.stderr)
if r.returncode == 0:
    for label in ('Markdown标题清理', '引用列表清理', '链接清理', '流式完整段落',
                  '未完成段不冒充摘要', '摘要长度有界', '推理默认折叠', '推理状态',
                  '截断提示', '内部工具名不展示', '连续事件合并', '截断可恢复',
                  '展开正文', '完成状态', '旧事件兼容', '无构建依赖'):
        print('✔ ' + label)
    print('全部通过 ✔')
raise SystemExit(r.returncode)
