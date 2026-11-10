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
const draftCount = 'document.querySelectorAll("[data-testid=reply-draft-quotes] .cwyc-reply-quote").length';
const action = 'document.querySelector("[data-testid=reply-to-selection]")';
async function select(selector, index = 0) {
  return js(`(() => { const el = document.querySelectorAll(${JSON.stringify(selector)})[${index}];
    if (!el) throw new Error('Missing selection fixture');
    el.scrollIntoView({block:'nearest'});
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
try {
  await rpc('Runtime.enable');
  await rpc('Emulation.setDeviceMetricsOverride', { width: 460, height: 900, deviceScaleFactor: 1, mobile: false });
  await until('!!window.chatUI && !!document.querySelector("[data-testid=composer-input]")');
  await new Promise((r) => setTimeout(r, 400)); // let the initial welcome timer register
  await js(`(() => { const original=window.pycmd; window.cwycSent=[];
    window.pycmd=(raw)=>{const m=JSON.parse(raw.slice(5)); if(m.type==='send')window.cwycSent.push(m);else original(raw);};
    window.pycmd('cwyc:'+JSON.stringify({type:'new_chat'}));
    window.chatUI.dispatch({type:'dock_state',expanded:true,width:460,side:'right',animating:false});
    window.chatUI.dispatch({type:'ui_config',suggested_questions:false}); })()`);
  await history(events);
  await type('Can you explain this more simply?');
  const quote = await select('[data-reply-message] [data-reply-text] p');
  await until(`!!${action}`);
  const bounds = await js(`(() => {const a=${action}.getBoundingClientRect();const v=document.querySelector('.cwyc-viewport').getBoundingClientRect();return a.left>=v.left && a.right<=v.right && a.top>=v.top && a.bottom<=v.bottom})()`);
  assert.ok(bounds, 'selection action stays inside dock');
  await screenshot('01-selection-action');
  await click('[data-testid=reply-to-selection]');
  await until(`${draftCount}===1`);
  await until("document.activeElement.dataset.testid === 'reply-comment'");
  assert.equal(await js('document.querySelector("[data-testid=composer-input]").value'), 'Can you explain this more simply?');
  assert.equal(await js('document.querySelector("[data-testid=reply-draft-quotes] blockquote").textContent'), quote);
  await type('Use an example.', '[data-testid=reply-comment]');
  // Duplicate selections return to the existing editable reply.
  await select('[data-reply-message] [data-reply-text] p');
  await until(`!!${action}`);
  await click('[data-testid=reply-to-selection]');
  await until(`${draftCount}===1`);
  // A second selection may come from another answer. Alt+Enter attaches it.
  const other = await select('[data-reply-message] [data-reply-text] p', 1);
  await until(`!!${action}`);
  await rpc('Input.dispatchKeyEvent', { type: 'keyDown', key: 'Enter', code: 'Enter', modifiers: 1 });
  await rpc('Input.dispatchKeyEvent', { type: 'keyUp', key: 'Enter', code: 'Enter', modifiers: 1 });
  await until(`${draftCount}===2`);
  await type('Why is this a prerequisite?', '[data-testid=reply-comment]', 1);
  await type('Use one concrete example.', '[data-testid=reply-comment]', 0);
  assert.equal(await js('document.querySelector("[data-testid=reply-draft-quotes] blockquote").textContent'), quote);
  await screenshot('02-quoted-draft');
  await screenshot('04-composer-detail', '.cwyc-composer');
  // Remove one without losing the draft, then add it back and send both.
  await click('[aria-label="Remove quoted selection 1"]');
  await until(`${draftCount}===1`);
  assert.equal(await js('document.querySelector("[data-testid=reply-draft-quotes] blockquote").textContent'), other);
  await select('[data-reply-message] [data-reply-text] p');
  await until(`!!${action}`); await click('[data-testid=reply-to-selection]');
  await type('Use one concrete example.', '[data-testid=reply-comment]', 1);
  await click('[data-testid=send]');
  await until('window.cwycSent.length===1');
  const sent = await js('window.cwycSent[0]');
  assert.equal(sent.text, 'Can you explain this more simply?');
  assert.deepEqual(sent.reply_quotes, [{text:other,source_turn:2,comment:'Why is this a prerequisite?'},{text:quote,source_turn:1,comment:'Use one concrete example.'}]);
  await until(`${draftCount}===0`);
  await until('document.querySelectorAll("[data-testid=sent-reply-quotes] .cwyc-reply-quote").length===2');
  await js("window.chatUI.dispatch({type:'done'})");
  // Saved user events reconstruct the quote UI; raw context tags are never shown.
  await history([...events,{type:'user_message',text:sent.text,reply_quotes:sent.reply_quotes},{type:'assistant_text',text:'Here is a simpler explanation.'}]);
  await until('document.querySelectorAll("[data-testid=sent-reply-quotes] .cwyc-reply-quote").length===2');
  assert.equal(await js('document.querySelectorAll("[data-testid=sent-reply-comment]").length'), 2);
  assert.equal(await js('document.querySelectorAll("[data-testid=reply-comment]").length'), 0);
  // User bubbles and reasoning/control surfaces do not offer the action.
  await select('[data-testid=user-message]');
  await until(`!${action}`);
  await history(events);
  await select('[data-reply-message] [data-reply-text] p'); await until(`!!${action}`);
  await rpc('Input.dispatchKeyEvent',{type:'keyDown',key:'Escape',code:'Escape'});
  await until(`!${action}`);
  // Per-selection comments can be sent without an additional main message.
  await select('[data-reply-message] [data-reply-text] p'); await until(`!!${action}`);
  await click('[data-testid=reply-to-selection]');
  await type('Can you expand this?', '[data-testid=reply-comment]');
  await type('');
  await until("!document.querySelector('[data-testid=send]').disabled");
  await js("document.querySelector('[data-testid=composer-input]').focus()");
  await rpc('Input.dispatchKeyEvent',{type:'keyDown',key:'Enter',code:'Enter'});
  await rpc('Input.dispatchKeyEvent',{type:'keyUp',key:'Enter',code:'Enter'});
  await until('window.cwycSent.length===2');
  const commentOnly = await js('window.cwycSent[1]');
  assert.equal(commentOnly.text, '');
  assert.equal(commentOnly.reply_quotes[0].comment, 'Can you expand this?');
  await js("window.chatUI.dispatch({type:'done'})");
  // Vim's empty composer can send staged comments as well.
  await js("window.chatUI.dispatch({type:'settings',vim_mode:true})");
  await until("!!document.querySelector('.cwyc-vim-editor .cm-content')");
  await select('[data-reply-message] [data-reply-text] p'); await until(`!!${action}`);
  await click('[data-testid=reply-to-selection]');
  await until("document.activeElement.dataset.testid === 'reply-comment'");
  await type('This is my editable reply.', '[data-testid=reply-comment]');
  await js("document.querySelector('.cwyc-vim-editor .cm-content').focus()");
  await rpc('Input.dispatchKeyEvent',{type:'keyDown',key:'Enter',code:'Enter'});
  await rpc('Input.dispatchKeyEvent',{type:'keyUp',key:'Enter',code:'Enter'});
  await until('window.cwycSent.length===3');
  await js("window.chatUI.dispatch({type:'done'})");
  await select('[data-reply-message] [data-reply-text] p'); await until(`!!${action}`);
  await click('[data-testid=reply-to-selection]');
  await type('Still editable before sending.', '[data-testid=reply-comment]');
  await js("window.chatUI.dispatch({type:'settings',vim_mode:false})");
  await until("!!document.querySelector('[data-testid=composer-input]')");
  await rpc('Emulation.setDeviceMetricsOverride',{width:360,height:760,deviceScaleFactor:1,mobile:false});
  await js("document.body.classList.add('night-mode')");
  await screenshot('03-narrow-dark-draft');
  assert.ok(await js('document.documentElement.scrollWidth<=innerWidth'), 'no horizontal overflow');
  await js("window.pycmd('cwyc:'+JSON.stringify({type:'new_chat'}))");
  await until(`${draftCount}===0`);
  assert.equal(errors.length,0,'no browser exceptions');
  console.log('Selection reply checks passed.');
} catch (error) {
  await screenshot("failure");
  await writeFile(resolve(output, "failure-dom.html"), await js("document.querySelector('.cwyc-viewport').innerHTML"));
  throw error;
} finally {
  socket.close();
  await fetch(`${endpoint}/json/close/${target.id}`).catch(()=>{});
}
