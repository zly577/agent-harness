import fs from 'node:fs'
import vm from 'node:vm'
import assert from 'node:assert/strict'
import { ref, watch, nextTick } from 'vue'
const source = fs.readFileSync(new URL('../src/App.vue', import.meta.url), 'utf8')
const query = source.split(/\r?\n/).find(line => line.includes('function fileSessionQuery()')) + '\n'
const load = source.slice(source.indexOf('    async function loadFiles()'), source.indexOf('    async function openWorkspaceFolder()'))
const watcher = source.slice(source.indexOf("    const WORKSPACE_ROOT = '/workspace'"), source.indexOf('    function openFilePanel()'))
const pending = []
const env = { ref, watch, sessionId:ref(null), activeWorkspaceId:ref('legacy'), showFilePanel:ref(true), fileList:ref([]), filePath:ref('/workspace'), loadingFiles:ref(false), pendingDelete:ref(null), apiGet:path=>new Promise((resolve,reject)=>pending.push({path,resolve,reject})) }
vm.createContext(env)
vm.runInContext('let filesViewVersion = 0;\n' + query + load + watcher, env)
const old = env.loadFiles()
assert.match(pending[0].path, /workspace_id=legacy/)
env.filePath.value = '/workspace/old-subdir'
env.pendingDelete.value = {path:'/workspace/old.txt'}
env.activeWorkspaceId.value = 'project-a'
await nextTick()
assert.equal(env.filePath.value, '/workspace')
assert.equal(env.pendingDelete.value, null)
assert.equal(env.fileList.value.length, 0)
assert.match(pending[1].path, /workspace_id=project-a/)
pending[1].resolve({entries:[{path:'/workspace/a.txt'}]})
await new Promise(resolve=>setImmediate(resolve))
pending[0].resolve({entries:[{path:'/workspace/legacy.txt'}]})
await old
assert.equal(env.fileList.value[0].path, '/workspace/a.txt')
env.sessionId.value = 'session-a'
await nextTick()
assert.match(pending[2].path, /workspace_id=project-a&session_id=session-a/)
env.activeWorkspaceId.value = 'project-b'
await nextTick()
pending[3].resolve({entries:[{path:'/workspace/b.txt'}]})
await new Promise(resolve=>setImmediate(resolve))
pending[2].reject(new Error('old request'))
await new Promise(resolve=>setImmediate(resolve))
assert.equal(env.fileList.value[0].path, '/workspace/b.txt')
assert.equal(env.loadingFiles.value, false)
console.log('File panel: selected workspace, session binding, refresh and stale responses passed')
