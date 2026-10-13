import fs from 'node:fs'
import vm from 'node:vm'
import assert from 'node:assert/strict'
const source = fs.readFileSync(new URL('../src/App.vue', import.meta.url), 'utf8')
const implementation = source.slice(source.indexOf('    async function newChat('), source.indexOf('    async function createWorkspace()'))

function context(rows, currentId, messageList) {
  const opened = [], created = []
  const env = { draftWorkspaceId: {value:"legacy"}, creatingChat: {value:false}, loadingHistory:{value:false}, sessionId:{value:currentId}, sessions:{value:rows}, messages:messageList, streaming:{value:false}, collapsedWorkspaces:{}, isSessionStreaming:()=>false, notify:message=>{throw new Error(message)}, openSession:async id=>opened.push(id), apiPost:async(path,body)=>{ created.push(body); return {id:'created',title:'新会话',workspace_id:body.workspace_id} } }
  env.resetChat = () => { env.sessionId.value = null; env.messages.splice(0); env.draftWorkspaceId.value = "legacy" }
  vm.createContext(env); vm.runInContext(implementation,env)
  return {env,opened,created}
}
let c=context([{id:'blank',title:'新会话',workspace_id:'legacy'}],'blank',[])
await c.env.newChat(); assert.equal(c.created.length,0)
c=context([],null,[]); await c.env.newChat(); assert.equal(c.created.length,0)
c=context([{id:'old',title:'任务',workspace_id:'legacy'}],'old',[{role:'user'}])
await Promise.all([c.env.newChat(),c.env.newChat()]); assert.equal(c.created.length,0)
c=context([{id:'old',title:'任务',workspace_id:'legacy'},{id:'unsent',title:'新会话',workspace_id:'project'}],'old',[{role:'user'}])
await c.env.newChat('project'); assert.equal(c.created.length,0); assert.deepEqual(c.opened,[]); assert.equal(c.env.draftWorkspaceId.value,'project')
c=context([{id:'blank',title:'新会话',workspace_id:'legacy'}],'blank',[])
await c.env.newChat('project'); assert.equal(c.created.length,0)
c=context([{id:'named-new',title:'新会话',workspace_id:'legacy'}],'named-new',[{role:'user',content:'已有对话'}])
await c.env.newChat(); assert.equal(c.created.length,0)
c=context([{id:'renamed-empty',title:'我的项目',workspace_id:'legacy'}],'renamed-empty',[])
await c.env.newChat(); assert.equal(c.created.length,0)
c=context([{id:'failed',title:'新会话',workspace_id:'legacy'}],'failed',[{role:'assistant',content:'加载失败'}])
await c.env.newChat(); assert.equal(c.created.length,0)
assert.equal(c.env.sessionId.value,null)
console.log('Draft conversations: 8 cases passed; no sidebar entry or API creation before sending')
