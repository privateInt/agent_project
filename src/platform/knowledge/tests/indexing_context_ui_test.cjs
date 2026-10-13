// Real shipped JS with DOM/network doubles: selection, late replies and mutation scope.
const vm=require('node:vm'),fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const {webcrypto}=require('node:crypto');
class Element{constructor(){this.value='';this.hidden=false;this.children=[];this.textContent='';this.listeners={};}append(...x){this.children.push(...x);}replaceChildren(...x){this.children=[...x];}addEventListener(k,v){this.listeners[k]=v;}focus(){}}
const deferred=()=>{let resolve;const promise=new Promise(r=>resolve=r);return {promise,resolve};};
function boot(){
 const elements=new Map(),requests=[],pauses=new Map();let list=[index('a'),index('b')];
 const get=id=>{if(!elements.has(id))elements.set(id,new Element());return elements.get(id);};
 async function fetch(url,options={}){
  requests.push({url,...options});let data;
  if(url==='/api/chunkers'||url.startsWith('/api/source-groups'))data={items:[]};
  else if(url.startsWith('/api/indexes?'))data={items:list};
  else if(url.endsWith('/versions?limit=20')){const id=url.split('/')[3];data={items:[{index_id:id,index_version_id:'v-'+id,state:'ready',chunk_count:1,indexed_count:1,reference_state:'held'}]};}
  else if(options.method)data=index(url.split('/')[3]||'a');
  else data=index(url.split('/')[3]);
  if(pauses.has(url)){const wait=pauses.get(url);pauses.delete(url);await wait.promise;}
  return {ok:true,status:200,json:async()=>data};
 }
 const c=vm.createContext({console,fetch,URLSearchParams,Option:class extends Element{constructor(text,value){super();this.textContent=text;this.value=value;}},crypto:webcrypto,confirm:()=>true,document:{getElementById:get,createElement:()=>new Element()},localStorage:{getItem:()=>null,setItem(){},removeItem(){}}});
 for(const file of ['app.js','indexing.js'])vm.runInContext(fs.readFileSync(path.join(__dirname,'../gateway/web',file),'utf8'),c);
 return {get,requests,pauses,run:code=>vm.runInContext(code,c),list:x=>{list=x;}};
}
function index(id){return {index_id:id,name:'index-'+id,state:'active',revision:1,active_version_id:''};}
(async()=>{
 let app=boot();await new Promise(setImmediate);
 app.get('indexes').value='a';const old=deferred();app.pauses.set('/api/indexes/a',old);
 const opening=app.run("openIndex('a')");
 app.get('indexes').value='b';app.get('indexes').listeners.change();await app.run("openIndex('b')");old.resolve();await opening;
 assert.equal(app.run('indexState.current.index_id'),'b');assert.equal(app.get('indexInfo').children[0].textContent,'index-b');
 assert.equal(app.get('indexVersions').children[0].children[1].textContent,'버전 v-b');
 app.get('indexes').value='a';const late=deferred();app.pauses.set('/api/indexes/a/versions?limit=20',late);
 const versions=app.run("openIndex('a')");await new Promise(setImmediate);
 const oldRemove=app.get('indexVersions').children[0];assert.equal(oldRemove,undefined);
 app.get('indexes').value='b';app.get('indexes').listeners.change();await app.run("openIndex('b')");late.resolve();await versions;
 assert.equal(app.get('indexVersions').children[0].children[1].textContent,'버전 v-b');
 const staleCard=app.get('indexVersions').children[0];const remove=staleCard.children.at(-1).children[1];
 app.get('indexes').value='a';app.get('indexes').listeners.change();
 await assert.rejects(app.run('(async()=>openedIndex())()'),/상세/);await remove.onclick();await app.get('indexDelete').listeners.click();
 assert.equal(app.requests.filter(x=>x.method).length,0,'changing selection must not mutate the previously opened index');
 app=boot();await new Promise(setImmediate);
 const listWait=deferred();app.pauses.set('/api/indexes?limit=20',listWait);app.list([index('old')]);const oldList=app.run('loadIndexes()');
 app.list([index('new')]);await app.run('loadIndexes()');listWait.resolve();await oldList;
 assert.equal(app.run("[...indexState.items.keys()].join(',')"),'new');
 app.get('indexes').value='a';await app.run("openIndex('a')");
 const mutateWait=deferred();app.pauses.set('/api/indexes/a/builds',mutateWait);
 const mutation=app.run("indexMutation('/api/indexes/a/builds',{chunking_run_id:'run'})");
 app.get('indexes').value='b';app.get('indexes').listeners.change();await app.run("openIndex('b')");mutateWait.resolve();await mutation;
 assert.equal(app.run('indexState.current.index_id'),'b','mutation acknowledgement must not reopen another selection');
 console.log('PASS management context: late index/version/list replies ignored; selection clears actions; stale card cannot delete; mutation acknowledgement preserves new selection.');
})().catch(e=>{console.error(e);process.exitCode=1;});
