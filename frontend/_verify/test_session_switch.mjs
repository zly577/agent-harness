import fs from 'node:fs'
import vm from 'node:vm'
import assert from 'node:assert/strict'
const source = fs.readFileSync(new URL('../src/App.vue', import.meta.url), 'utf8')
const code = source.slice(source.indexOf('    async function openSession(id)'), source.indexOf('    async function newChat('))
const pending = []
const env = { sessionViewVersion:0, sessionStates:{}, messages:[{content:'previous'}], loadingHistory:{value:false}, stickToBottom:{}, hasNewBelow:{}, approval:{value:{old:true}}, reactive:x=>x, noteBlankTail:x=>x, syncProviderFromSession:()=>{}, scrollToBottom:()=>{}, resumeRun:()=>{}, restorePendingApproval:()=>{}, nextTick:async()=>{}, isModelMissingError:()=>false, apiGet:path=>new Promise((resolve,reject)=>pending.push({path,resolve,reject})) }
let selected=null
Object.defineProperty(env,'sessionId',{value:{get value(){return selected},set value(id){selected=id;env.sessionViewVersion++;env.loadingHistory.value=false}}})
vm.createContext(env);vm.runInContext(code,env)
let a=env.openSession('A');assert.equal(env.messages.length,0);assert.equal(env.loadingHistory.value,true)
let b=env.openSession('B');pending[1].resolve([{role:'user',content:'B'}]);await b
pending[0].resolve([{role:'user',content:'A'}]);await a
assert.equal(env.messages[0].content,'B');assert.equal(env.loadingHistory.value,false)
a=env.openSession('A');b=env.openSession('B');await b
assert.equal(env.loadingHistory.value,false)
pending[2].reject(new Error('stale failure'));await a;assert.equal(env.messages[0].content,'B')
a=env.openSession('A');const c=env.openSession('C');const again=env.openSession('A')
pending[5].resolve([{role:'user',content:'new A'}]);await again
pending[3].resolve([{role:'user',content:'old A'}]);await a
pending[4].resolve([{role:'user',content:'C'}]);await c
assert.equal(env.messages[0].content,'new A')
const d=env.openSession('D');env.sessionId.value=null;env.messages.splice(0)
pending[6].resolve([{role:'user',content:'D'}]);await d;assert.equal(env.messages.length,0)
console.log('Session switching: slow responses, cached switch, stale errors, A-B-A and reset passed')
