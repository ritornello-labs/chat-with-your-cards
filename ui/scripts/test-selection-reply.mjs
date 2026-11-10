/** Synthetic UI integration checks. Use a dedicated headless Chrome profile.
 * Start the dev replayer, then pass its URL and Chrome's CDP HTTP endpoint.
 * No packages, accounts or Anki collection are used. Captures stay outside Git.
 */
import assert from 'node:assert/strict';
import { mkdir, writeFile } from 'node:fs/promises';
import { resolve } from 'node:path';

const [url = 'http://127.0.0.1:5194', endpoint = 'http://127.0.0.1:9334', output = '/tmp/cwyc-selection-reply-checks'] = process.argv.slice(2);
const target = await (await fetch(`${endpoint}/json/new?${encodeURIComponent(url)}`, { method: 'PUT' })).json();
const socket = new WebSocket(target.webSocketDebuggerUrl);
await new Promise((yes, no) => { socket.onopen = yes; socket.onerror = no; });
let nextId = 0;
const pending = new Map();
const errors = [];
socket.onmessage = (e) => {
  const m = JSON.parse(e.data);
  if (m.method === 'Runtime.exceptionThrown') errors.push(m.params.exceptionDetails.text);
  const p = pending.get(m.id);
  if (p) { pending.delete(m.id); m.error ? p.reject(new Error(m.error.message)) : p.resolve(m.result); }
};
async function rpc(method, params = {}) {
  const id = ++nextId;
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => { pending.delete(id); reject(new Error(`Timed out: ${method}`)); }, 15000);
    pending.set(id, { resolve: (v) => { clearTimeout(timer); resolve(v); }, reject: (e) => { clearTimeout(timer); reject(e); } });
    socket.send(JSON.stringify({ id, method, params }));
  });
}
async function js(expression) {
  const r = await rpc('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true });
  if (r.exceptionDetails) throw new Error(r.exceptionDetails.exception?.description || r.exceptionDetails.text);
  return r.result.value;
}
async function until(expression) {
  for (let i = 0; i < 80; i++) {
    if (await js(expression)) return;
    await new Promise((r) => setTimeout(r, 50));
  }
  throw new Error(`UI condition timed out: ${expression}`);
}
const action = 'document.querySelector("[data-testid=reply-to-selection]")';
async function select(selector, index = 0) {
  await js(`(() => {document.activeElement?.blur();document.querySelectorAll(${JSON.stringify(selector)})[${index}].scrollIntoView({block:'center'});})()`);
  await js('new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))');
  return js(`(() => { const el = document.querySelectorAll(${JSON.stringify(selector)})[${index}];
    if (!el) throw new Error('Missing selection fixture');
    const r = document.createRange(); r.selectNodeContents(el);
    const s = getSelection(); s.removeAllRanges(); s.addRange(r);
    document.dispatchEvent(new Event('selectionchange'));
    return s.toString(); })()`);
}
async function click(selector) {
  await until(`!!document.querySelector(${JSON.stringify(selector)})`);
  const point = await js(`(() => {const r=document.querySelector(${JSON.stringify(selector)}).getBoundingClientRect();return {x:r.x+r.width/2,y:r.y+r.height/2}})()`);
  await rpc('Input.dispatchMouseEvent', { type: 'mousePressed', button: 'left', clickCount: 1, ...point });
  await rpc('Input.dispatchMouseEvent', { type: 'mouseReleased', button: 'left', clickCount: 1, ...point });
}
async function type(text, selector = '[data-testid=composer-input]', index = 0) {
  await js(`(() => { const el=document.querySelectorAll(${JSON.stringify(selector)})[${index}];
    Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype,'value').set.call(el,${JSON.stringify(text)});
    el.dispatchEvent(new Event('input',{bubbles:true})); })()`);
}
async function screenshot(name, selector) {
  await mkdir(output, { recursive: true });
  const clip = selector ? await js(`(() => {const r=document.querySelector(${JSON.stringify(selector)}).getBoundingClientRect();return {x:r.x,y:r.y,width:r.width,height:r.height,scale:1}})()`) : undefined;
  const { data } = await rpc('Page.captureScreenshot', { format: 'png', ...(clip ? {clip} : {}) });
  await writeFile(resolve(output, `${name}.png`), Buffer.from(data, 'base64'));
}
async function annotationScreenshot(name) {
  const clip = await js(`(() => {const nodes=[document.querySelector('[data-testid=annotation-editor]'),document.querySelector('[data-reply-message]'),document.querySelector('[data-testid=annotation-marker]')];const boxes=nodes.map(n=>n.getBoundingClientRect());const x=Math.max(0,Math.min(...boxes.map(r=>r.left))-6),y=Math.max(0,Math.min(...boxes.map(r=>r.top))-6);return {x,y,width:Math.min(innerWidth-x,Math.max(...boxes.map(r=>r.right))-x+6),height:Math.min(innerHeight-y,Math.max(...boxes.map(r=>r.bottom))-y+6),scale:1}})()`);
  const {data}=await rpc('Page.captureScreenshot',{format:'png',clip});
  await writeFile(resolve(output,`${name}.png`),Buffer.from(data,'base64'));
}
async function history(events) {
  await js(`window.chatUI.dispatch({type:'history_load',events:${JSON.stringify(events)}})`);
  await until('document.querySelectorAll("[data-testid=assistant-message]").length > 0');
}
const first = 'A **single** explanation can contain several ideas. 中文 🙂 stays intact.';
const second = 'The second passage explains **why the prerequisite matters**.\n\n`Quoted code <tag>` stays plain text.';
const events = [
  {type:'user_message',text:'Explain this card'}, {type:'assistant_text',text:first},
  {type:'user_message',text:'What should I review?'}, {type:'assistant_text',text:second},
];
const editor = "document.querySelector('[data-testid=annotation-editor]')";
const longComment = 'BLABLABLABLABLALL'.repeat(60);
async function saveAnnotation() {
  await click('[data-testid=save-annotation]');
  await until(`!${editor}`);
}
async function addSelection(index = 0) {
  const text = await select('[data-reply-message] [data-reply-text] p', index);
  await until(`!!${action}`);
  await click('[data-testid=reply-to-selection]');
  await until(`!!${editor}`);
  await until("document.activeElement.dataset.testid === 'reply-comment'");
  return text;
}
try {
  await rpc('Runtime.enable');
  await rpc('Emulation.setDeviceMetricsOverride', { width: 460, height: 900, deviceScaleFactor: 1, mobile: false });
  await until('!!window.chatUI && !!document.querySelector("[data-testid=composer-input]")');
  await new Promise((r) => setTimeout(r, 400));
  await js(`(() => { const original=window.pycmd; window.cwycSent=[];
    window.pycmd=(raw)=>{const m=JSON.parse(raw.slice(5));if(m.type==='send')window.cwycSent.push(m);else original(raw);};
    window.pycmd('cwyc:'+JSON.stringify({type:'new_chat'}));
    window.chatUI.dispatch({type:'dock_state',expanded:true,width:460,side:'right',animating:false});
    window.chatUI.dispatch({type:'ui_config',suggested_questions:false}); })()`);
  await history(events);
  await type('Please address these points.');
  const quote = await select('[data-reply-message] [data-reply-text] p');
  await until(`!!${action}`);
  assert.equal(await js(`${action}.textContent`), 'Add to chat');
  await screenshot('01-add-to-chat');
  await click('[data-testid=reply-to-selection]');
  await until(`!!${editor}`);
  await until("document.activeElement.dataset.testid==='reply-comment'");
  assert.equal(await js('document.querySelector(".cwyc-composer [data-testid=reply-comment]")'), null);
  assert.equal(await js('document.querySelector("[data-testid=composer-input]").value'), 'Please address these points.');
  assert.equal(await js('document.querySelector("[data-testid=send]").disabled'), true);
  await type('Use a concrete example.', '[data-testid=reply-comment]');
  await screenshot('02-inline-new-comment');
  await saveAnnotation();
  await until("document.querySelectorAll('[data-testid=annotation-marker]').length===1");
  assert.equal(await js("document.querySelector('[data-testid=annotation-summary]').textContent.trim()"), '1 annotation');
  assert.ok(await js("CSS.highlights.get('cwyc-annotations').size===1"));
  await screenshot('03-saved-marker');
  // Marker reopens the saved comment. Cancel discards the edit.
  await click('[aria-label="Edit annotation 1"]');
  await until(`!!${editor}`);
  assert.equal(await js("document.querySelector('[data-testid=reply-comment]').value"), 'Use a concrete example.');
  await type(longComment, '[data-testid=reply-comment]');
  await screenshot('04-long-comment-editor');
  await annotationScreenshot('04-inline-editor-detail');
  const geometry = await js(`(() => {const p=${editor}.getBoundingClientRect();const v=document.querySelector('.cwyc-viewport').getBoundingClientRect();const t=document.querySelector('[data-testid=reply-comment]');return {within:p.left>=v.left&&p.right<=v.right&&p.top>=v.top&&p.bottom<=v.bottom,wrap:t.scrollWidth<=t.clientWidth,scroll:t.scrollHeight>t.clientHeight}})()`);
  assert.ok(geometry.within && geometry.wrap && geometry.scroll, 'long comments wrap and scroll inside the anchored panel');
  await click('[data-testid=cancel-annotation]');
  await until(`!${editor}`);
  await click('[aria-label="Edit annotation 1"]');
  await until(`!!${editor}`);
  assert.equal(await js("document.querySelector('[data-testid=reply-comment]').value"), 'Use a concrete example.');
  await type(longComment, '[data-testid=reply-comment]');
  await saveAnnotation();
  // Save persists independently; the compact chip reopens it.
  await click('[data-testid=annotation-summary]'); await until(`!!${editor}`);
  assert.equal(await js("document.querySelector('[data-testid=reply-comment]').value"), longComment);
  await click('[data-testid=cancel-annotation]'); await until(`!${editor}`);
  const other = await addSelection(1);
  await type('Why is this a prerequisite?', '[data-testid=reply-comment]');
  await saveAnnotation();
  await until("document.querySelectorAll('[data-testid=annotation-marker]').length===2");
  await screenshot('05-two-annotations-compact');
  await screenshot('05-compact-composer-detail', '.cwyc-composer');
  // The summary menu addresses annotations individually. Delete removes one.
  await click('[data-testid=annotation-summary]');
  await until("document.querySelectorAll('[role=menuitem]').length===2");
  await click('[role=menuitem]:last-child'); await until(`!!${editor}`);
  assert.equal(await js("document.querySelector('[data-testid=reply-comment]').value"), 'Why is this a prerequisite?');
  await click('[data-testid=delete-annotation]'); await until(`!${editor}`);
  await until("document.querySelectorAll('[data-testid=annotation-marker]').length===1");
  await addSelection(1);
  await type('Unsaved temporary comment', '[data-testid=reply-comment]');
  await rpc('Input.dispatchKeyEvent', {type:'keyDown',key:'Escape',code:'Escape'});
  await until(`!${editor}`);
  assert.equal(await js("document.querySelector('[data-testid=annotation-summary]').textContent.trim()"), '1 annotation');
  await addSelection(1); await type('Why is this a prerequisite?', '[data-testid=reply-comment]'); await saveAnnotation();
  // All saved comments can be sent with no separate message.
  await type('');
  await click('[data-testid=send]'); await until('window.cwycSent.length===1');
  const sent = await js('window.cwycSent[0]');
  assert.equal(sent.text, '');
  assert.deepEqual(sent.reply_quotes,[{text:quote,source_turn:1,comment:longComment},{text:other,source_turn:2,comment:'Why is this a prerequisite?'}]);
  await until("document.querySelectorAll('[data-testid=annotation-marker]').length===0");
  await js("window.chatUI.dispatch({type:'done'})");
  await history([...events,{type:'user_message',text:'',reply_quotes:sent.reply_quotes},{type:'assistant_text',text:'Here are both explanations.'}]);
  await until("document.querySelectorAll('[data-testid=sent-reply-comment]').length===2");
  // Vim can send saved annotations from an empty main composer.
  await js("window.chatUI.dispatch({type:'settings',vim_mode:true})");
  await until("!!document.querySelector('.cwyc-vim-editor .cm-content')");
  await addSelection(); await type('A saved Vim reply.', '[data-testid=reply-comment]'); await saveAnnotation();
  await js("document.querySelector('.cwyc-vim-editor .cm-content').focus()");
  await rpc('Input.dispatchKeyEvent',{type:'keyDown',key:'Enter',code:'Enter'});
  await rpc('Input.dispatchKeyEvent',{type:'keyUp',key:'Enter',code:'Enter'});
  await until('window.cwycSent.length===2'); await js("window.chatUI.dispatch({type:'done'})");
  await js("window.chatUI.dispatch({type:'settings',vim_mode:false})");
  await until("!!document.querySelector('[data-testid=composer-input]')");
  await history(events);
  await rpc('Emulation.setDeviceMetricsOverride',{width:360,height:760,deviceScaleFactor:1,mobile:false});
  await js("document.body.classList.add('night-mode')");
  await addSelection(); await type(longComment, '[data-testid=reply-comment]'); await saveAnnotation();
  await click('[data-testid=annotation-summary]'); await until(`!!${editor}`);
  await screenshot('06-narrow-dark-edit');
  assert.ok(await js('document.documentElement.scrollWidth<=innerWidth'), 'no horizontal overflow');
  await js("window.pycmd('cwyc:'+JSON.stringify({type:'new_chat'}))");
  await until(`!${editor}`);
  await until("!document.querySelector('[data-testid=annotation-summary]')");
  assert.equal(errors.length,0,'no browser exceptions');
  console.log('Inline annotation checks passed.');
} catch (error) {
  await screenshot('failure');
  await writeFile(resolve(output,'failure-dom.html'),await js("document.querySelector('.cwyc-thread').innerHTML"));
  throw error;
} finally {
  socket.close();
  await fetch(`${endpoint}/json/close/${target.id}`).catch(()=>{});
}
