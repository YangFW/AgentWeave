"""以 Node 执行前端会话门禁，验证登录成功前不进入业务初始化。"""
from pathlib import Path
import subprocess
import unittest


class FrontendAuthTests(unittest.TestCase):
    def test_custom_idempotency_header_keeps_json_content_type(self):
        root = Path(__file__).resolve().parents[1]
        script = r'''
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const source=fs.readFileSync('web/app.js','utf8');
const calls=[];
const context={FormData:class {},fetch:async(path,options)=>{calls.push(options);return {ok:true,status:200,json:async()=>({id:'task-a'})};}};
vm.createContext(context);
vm.runInContext(source.slice(source.indexOf('async function api('),source.indexOf('function escapeHtml(')),context);
(async()=>{
 await context.api('/api/tasks',{method:'POST',headers:{'Idempotency-Key':'stable'},body:'{}'});
 assert.equal(calls[0].headers['Content-Type'],'application/json');
 assert.equal(calls[0].headers['Idempotency-Key'],'stable');
})().catch(e=>{console.error(e);process.exitCode=1;});
'''
        result = subprocess.run(['node','-e',script], cwd=root, capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)

    def test_user_editor_does_not_resubmit_unchanged_password(self):
        root = Path(__file__).resolve().parents[1]
        script = r'''
const fs = require('node:fs'), vm = require('node:vm'), assert = require('node:assert/strict');
const source = fs.readFileSync('web/app.js','utf8');
const code = source.slice(source.indexOf('function selectAdminUser('), source.indexOf('async function initializeAuthentication('));
const elements = new Map(), calls = [];
const user = {id:'u-1', username:'alice', role:'user', enabled:true};
const context = {state:{adminUsers:[],adminSelectedUser:null}, encodeURIComponent,
  $: id => {if (!elements.has(id)) elements.set(id,{value:'',checked:false,replaceChildren(){},appendChild(){}}); return elements.get(id);},
  document:{createElement:()=>({})}, notify(){},
  api:async(path,options)=>{calls.push({path,options}); return options ? user : [user];}
};
vm.createContext(context); vm.runInContext(code,context);
(async()=>{
  context.selectAdminUser(null);
  context.$('adminUsername').value='alice';
  context.$('adminUserPassword').value='new-password-for-testing';
  await context.saveAdminUser({preventDefault(){}});
  assert.equal(calls[0].options.method,'POST');
  assert.equal(JSON.parse(calls[0].options.body).password,'new-password-for-testing');
  assert.equal(context.$('adminUserPassword').value,'');
  assert.equal(context.$('adminUsername').disabled,true);
  calls.length=0;
  context.$('adminUserEnabled').checked=false;
  await context.saveAdminUser({preventDefault(){}});
  assert.equal(calls[0].path,'/api/users/u-1');
  const update=JSON.parse(calls[0].options.body);
  assert.equal(update.enabled,false);
  assert.equal(Object.hasOwn(update,'password'),false);
  assert.equal(JSON.stringify(context.state).includes('new-password-for-testing'),false);
})().catch(e=>{console.error(e);process.exitCode=1;});
'''
        result = subprocess.run(['node', '-e', script], cwd=root, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_session_gate_login_logout_and_account_change(self):
        root = Path(__file__).resolve().parents[1]
        script = r'''
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('web/app.js', 'utf8');
const fn = source.slice(source.indexOf('async function initializeAuthentication()'), source.indexOf('(async function init()'));
function fixture(session) {
  const elements = new Map();
  function element(id) {
    if (!elements.has(id)) elements.set(id, {value: '', textContent: '', disabled: false, focus() {}, classList: {remove() {}}});
    return elements.get(id);
  }
  const calls = [], prefs = new Map();
  const context = {state: {}, $: element, revealed: false, reloads: 0,
    api: async (path, options) => {calls.push({path, options}); return path === '/api/auth/me' ? session : {authenticated: true};},
    readPreference: key => prefs.get(key), writePreference: (key, val) => prefs.set(key, val),
    createConversationId: () => 'fresh-conversation', notify() {},
  };
  context.location = {reload() {context.reloads++;}};
  context.document = {body: {classList: {remove() {context.revealed = true;}}}};
  vm.createContext(context); vm.runInContext(fn, context);
  return {context, elements, calls, prefs};
}
(async () => {
  let f = fixture({enabled: true, authenticated: false});
  assert.equal(await f.context.initializeAuthentication(), false);
  assert.equal(f.context.revealed, false);
  f.context.$('loginUsername').value = 'alice';
  f.context.$('loginPassword').value = 'test-password';
  await f.elements.get('loginForm').onsubmit({preventDefault() {}});
  assert.equal(f.calls[1].path, '/api/auth/login');
  assert.equal(f.context.reloads, 1);
  assert.equal(f.prefs.size, 0); // 密码、Cookie 不写入 localStorage。

  f = fixture({enabled: true, authenticated: true, user: {username: 'alice', user_id: 'u-1'}});
  assert.equal(await f.context.initializeAuthentication(), true);
  assert.equal(f.context.revealed, true);
  assert.equal(f.context.state.conversationId, 'fresh-conversation');
  assert.equal(f.elements.get('accountName').textContent, 'alice');
  await f.elements.get('logoutButton').onclick();
  assert.equal(f.calls[1].path, '/api/auth/logout');

  f = fixture({enabled: false, authenticated: false});
  assert.equal(await f.context.initializeAuthentication(), true);
  assert.equal(f.context.revealed, true);
})().catch(error => {console.error(error); process.exitCode = 1;});
'''
        result = subprocess.run(['node', '-e', script], cwd=root, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
