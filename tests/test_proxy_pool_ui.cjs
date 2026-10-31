const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
async function flush(){for(let i=0;i<16;i++)await Promise.resolve();}

// 抽出 index.html 里的代理池管理脚本，用最小的假 DOM 运行。
function harness() {
  const source=fs.readFileSync('webui/templates/index.html','utf8');
  const block=source.match(/\/\/ ---------- 代理池管理 ----------[\s\S]*?(?=\/\/ ---------- 全局复制委托 ----------)/)[0];
  const elements=new Map(),requests=[],toasts=[];
  const element=id=>{
    const listeners={};
    const classes=new Set(['hidden']);
    const el={id,value:'',textContent:'',innerHTML:'',className:'',type:'text',checked:false,disabled:false,title:'',dataset:{},
      classList:{add:c=>classes.add(c),remove:c=>classes.delete(c),contains:c=>classes.has(c),toggle:(c,f)=>((f??!classes.has(c))?classes.add(c):classes.delete(c))},
      addEventListener(type,fn){(listeners[type]||=[]).push(fn);},
      fire(type){(listeners[type]||[]).forEach(fn=>fn({target:el,key:''}));},
      setAttribute(){},focus(){},matches(){return false;}};
    return el;
  };
  const $=sel=>{if(!elements.has(sel))elements.set(sel,element(sel));return elements.get(sel);};
  const context=vm.createContext({
    $,document:{getElementById:()=>null,addEventListener(){}},
    api(url,opts){return new Promise((resolve,reject)=>requests.push({url,body:JSON.parse(opts?.body||'null'),resolve,reject}));},
    debounce:fn=>fn,setTimeout:()=>0,showToast:m=>toasts.push(m),updateModalScrollLock(){},copyText(){},confirm:()=>true,
    esc:v=>String(v??''),attrEsc:v=>String(v??''),
  });
  vm.runInContext(block,context);
  const run=code=>vm.runInContext(code,context);
  return {run,$,requests,toasts,
    async resolve(i,value){requests[i].resolve(value);await flush();}};
}
const row=(id,raw,extra={})=>({id,raw,key:'',name:'',enabled:!raw.startsWith('#'),url:'',valid:true,scheme:'http',host:'h',port:80,username:'',password:'',test:null,...extra});
// 某个 await 永远不返回时，Node 会在事件循环清空后以 0 退出；没跑到结尾就判失败。
let finished=false;
process.on('exit',()=>{if(!finished){console.error('proxy pool ui test did not finish');process.exitCode=1;}});

(async()=>{
  // 快速填写：与后端一致地识别完整 URL（含 #名称、IPv6）和三种简写。
  const h=harness();
  const parse=text=>JSON.parse(h.run(`JSON.stringify(ppParseProxyText(${JSON.stringify(text)}))`));
  assert.deepEqual(parse('socks5h://bob:pa%23ss@127.0.0.1:11081#美国 1'),
    {scheme:'socks5h',host:'127.0.0.1',port:'11081',username:'bob',password:'pa#ss',name:'美国 1'});
  assert.equal(parse('http://[2001:db8::1]:8080').host,'2001:db8::1');
  assert.deepEqual(parse('1.2.3.4:3128:user:pa:ss'),{scheme:'',host:'1.2.3.4',port:'3128',username:'user',password:'pa:ss',name:''});
  assert.equal(parse('user:pass:1.2.3.4:3128').host,'1.2.3.4');
  assert.equal(parse('1.2.3.4:3128').port,'3128');
  assert.equal(parse('nonsense'),null);

  // 国旗：合法国家代码用固定版本的 flag-icons 图片；加载失败过的代码和非两位代码直接显示文字。
  assert.match(h.run("ppFlagHtml('us')"),/<img class="pp-flag" src="https:\/\/unpkg\.com\/flag-icons@7\.6\.0\/flags\/4x3\/us\.svg"/);
  assert.match(h.run("ppFlagHtml('us')"),/referrerpolicy="no-referrer"/);
  h.run("PP_FLAG_FAILED.add('JP')");
  assert.equal(h.run("ppFlagHtml('jp')"),'<span class="pp-cc">JP</span>');
  assert.equal(h.run("ppFlagHtml('T1')"),'<span class="pp-cc">T1</span>');
  assert.equal(h.run("ppFlagHtml('')"),'');

  // 表单拼 URL：IPv6 加方括号，账号密码按原样编码（不去首尾空格）。
  h.$('#proxyEditScheme').value='socks5';h.$('#proxyEditHost').value='2001:db8::1';h.$('#proxyEditPort').value='1080';
  h.$('#proxyEditUser').value=' u ';h.$('#proxyEditPass').value='p@ss word';
  assert.equal(h.run('ppEditFormUrl()'),'socks5://%20u%20:p%40ss%20word@[2001:db8::1]:1080');

  // 同一地址的多行各有独立的勾选标识，勾选一行不会连带另一行。
  h.run(`applyProxyPoolResponse({version:'v1',items:[${JSON.stringify(row(0,'http://a:1#甲',{key:'k'}))},${JSON.stringify(row(1,'http://a:1#乙',{key:'k'}))}]})`);
  assert.notEqual(h.run('ppIdentity(PROXY_POOL_STATE.items[0])'),h.run('ppIdentity(PROXY_POOL_STATE.items[1])'));
  h.run('PROXY_POOL_STATE.selected.add(ppIdentity(PROXY_POOL_STATE.items[0]))');
  assert.equal(h.run('PROXY_POOL_STATE.selected.has(ppIdentity(PROXY_POOL_STATE.items[1]))'),false);

  // 较早生成的列表响应不能覆盖页面上更新的测试结果；更新的服务端结果则正常替换。
  h.run(`applyProxyPoolResponse({version:'v2',items:[${JSON.stringify(row(0,'http://a:1',{key:'k',test:{ok:true,latency_ms:100,tested_at:'2026-10-31T18:00:10+08:00'}}))}]})`);
  h.run(`applyProxyPoolResponse({version:'v2',items:[${JSON.stringify(row(0,'http://a:1',{key:'k',test:{ok:true,latency_ms:999,tested_at:'2026-10-31T18:00:00+08:00'}}))}]})`);
  assert.equal(h.run('PROXY_POOL_STATE.items[0].test.latency_ms'),100);
  h.run(`applyProxyPoolResponse({version:'v2',items:[${JSON.stringify(row(0,'http://a:1',{key:'k',test:{ok:false,tested_at:'2026-10-31T18:01:00+08:00'}}))}]})`);
  assert.equal(h.run('PROXY_POOL_STATE.items[0].test.ok'),false);

  // 冲突刷新后行号变化：编辑仍按原始行定位，目标被删除时拒绝保存，不能覆盖别的代理。
  const A=row(0,'http://1.1.1.1:80#A',{key:'ka'}),B=row(1,'http://2.2.2.2:80#B',{key:'kb'});
  h.run(`applyProxyPoolResponse({version:'v3',items:[${JSON.stringify(A)},${JSON.stringify(B)}]})`);
  h.run('openProxyEditModal(PROXY_POOL_STATE.items[0])');
  h.$('#proxyEditName').value='A2';h.$('#proxyEditScheme').value='http';h.$('#proxyEditHost').value='1.1.1.1';
  h.$('#proxyEditPort').value='80';h.$('#proxyEditUser').value='';h.$('#proxyEditPass').value='';h.$('#proxyEditEnabled').checked=true;
  h.run(`applyProxyPoolResponse({version:'v4',items:[${JSON.stringify({...B,id:0})}]})`);
  const before=h.requests.length;
  h.run('saveProxyEditForm()');await flush();
  assert.equal(h.requests.length,before,'deleted edit target must not be saved over another proxy');
  assert.match(h.$('#proxyEditTestResult').textContent,/已在其他地方被修改或删除/);
  h.run(`applyProxyPoolResponse({version:'v5',items:[${JSON.stringify({...B,id:0})},${JSON.stringify({...A,id:1})}]})`);
  h.run('saveProxyEditForm()');await flush();
  const save=h.requests.at(-1);
  assert.equal(save.url,'/api/proxy-pool');
  assert.deepEqual(save.body.items,[{raw:B.raw},{name:'A2',url:'http://1.1.1.1:80',enabled:true}]);
  assert.equal(save.body.version,'v5');
  await h.resolve(h.requests.length-1,{version:'v6',items:[]});

  // 弹窗测试结果回来前表单已改：结论作废，不能把旧地址的结果显示给新地址。
  h.run('openProxyEditModal(null)');
  h.$('#proxyEditScheme').value='http';h.$('#proxyEditHost').value='3.3.3.3';h.$('#proxyEditPort').value='8080';
  h.run('testProxyEditForm()');await flush();
  const pending=h.requests.length-1;
  assert.equal(h.requests[pending].url,'/api/proxy-pool/test');
  h.$('#proxyEditHost').value='4.4.4.4';h.$('#proxyEditHost').fire('input');
  await h.resolve(pending,{key:'k3',result:{ok:true,latency_ms:321,ip:'8.8.8.8',tested_at:'2026-10-31T18:02:00+08:00'}});
  assert.equal(h.$('#proxyEditTestResult').textContent,'');
  // 未改动时正常显示结论。
  h.run('testProxyEditForm()');await flush();
  await h.resolve(h.requests.length-1,{key:'k4',result:{ok:true,latency_ms:321,ip:'8.8.8.8',tested_at:'2026-10-31T18:03:00+08:00'}});
  assert.match(h.$('#proxyEditTestResult').textContent,/连接正常 · 延迟 321 ms/);
  finished=true;
  console.log('proxy pool ui ok');
})().catch(error=>{console.error(error);process.exit(1);});
