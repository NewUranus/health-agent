/* 健康对话、逐节点 SSE 与可追溯来源；所有外部文本先转义。 */
const $ = (selector) => document.querySelector(selector);
const createId = () => globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random().toString(36).slice(2)}`;
let userId;
try { userId = localStorage.getItem('healthpilot-user') || createId(); localStorage.setItem('healthpilot-user', userId); }
catch { userId = createId(); }
let threadId = createId();
let busy = false;
let turn = 0;
let nodeCount = 0;
let sourceIndex = 0;
const sourceNumbers = new Map();
const sourceElements = new Map();
const labels = {sex:'性别',age:'年龄',sbp:'收缩压',dbp:'舒张压',total_chol:'总胆固醇',hdl:'HDL',smoker:'吸烟',diabetes:'糖尿病',bp_treated:'服降压药',height_cm:'身高',weight_kg:'体重'};
const nodes = {safety_check:'急症筛查',emergency:'紧急就医指引',intake:'整理健康档案',supervisor:'规划下一步',ask_missing:'补充问诊',risk_assess:'计算健康风险',retrieve:'检索指南依据',advise:'生成干预建议',verify:'校验建议与引用',followup:'制定随访计划',simulate:'模拟干预效果',checkin:'对比复查结果',answer:'回答健康问题'};
const agents = {orchestrator:['协调 Agent','#82988b'],intake_agent:['问诊 Agent','#82a1ac'],assessment_agent:['评估 Agent','#6484ac'],intervention_agent:['干预 Agent','#6ba28b'],followup_agent:['随访 Agent','#ad986c'],simulation_agent:['模拟 Agent','#9784b2'],checkin_agent:['复查 Agent','#b08c74'],qa_agent:['问答 Agent','#7c9aaf']};
const escapeHTML = (text) => String(text).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function inline(text, turnId) {
  return escapeHTML(text).replace(/\*\*(.+?)\*\*/g,'<strong>$1</strong>').replace(/\[cite:([\w-]+)\]/g, (_, id) => {
    const key = `${turnId}:${id}`;
    if (!sourceNumbers.has(key)) sourceNumbers.set(key, ++sourceIndex);
    return `<button class="citation" data-source="${key}" title="查看引用来源 ${escapeHTML(id)}" aria-label="查看引用来源 ${escapeHTML(id)}">${sourceNumbers.get(key)}</button>`;
  });
}
function markdown(text, turnId) {
  let html = '', inList = false;
  for (const line of text.split('\n')) {
    const item = /^\s*(?:- |\d+[.、] )(.+)$/.exec(line);
    if (!item && inList) { html += '</ul>'; inList = false; }
    if (item) { if (!inList) { html += '<ul>'; inList = true; } html += `<li>${inline(item[1], turnId)}</li>`; }
    else if (/^#{1,3} /.test(line)) html += `<h3>${inline(line.replace(/^#{1,3} /,''), turnId)}</h3>`;
    else if (/^> ?/.test(line)) html += `<blockquote>${inline(line.replace(/^> ?/,''), turnId)}</blockquote>`;
    else if (line.trim()) html += `<p>${inline(line, turnId)}</p>`;
  }
  return html + (inList ? '</ul>' : '');
}
function addMessage(text, role='assistant', turnId=turn) {
  const message = document.createElement('article'); message.className = `message ${role}`;
  message.innerHTML = `${role !== 'user' ? '<span class="avatar" aria-hidden="true">✚</span>' : ''}<div class="message-body"><div class="message-label">${role === 'user' ? '你' : 'HealthPilot'}</div><div class="bubble">${role === 'user' ? escapeHTML(text) : markdown(text, turnId)}</div></div>`;
  $('#messages').append(message); $('#messages').scrollTop = $('#messages').scrollHeight;
}
function welcome() {
  addMessage('你好，我是 HealthPilot。\n我们可以一起了解你的心血管健康，从年龄、性别和最近的血压开始。资料齐全后，我会提供风险评估、有来源的建议和随访计划。\n也可以点击下方示例，体验完整流程。');
}
function showProfile(data) {
  const entries = Object.entries(data.profile || {});
  $('#profile').innerHTML = entries.map(([key,value]) => `<div class="field"><span>${labels[key] || escapeHTML(key)}</span><b>${typeof value === 'boolean' ? (value ? '是' : '否') : escapeHTML(value === 'male' ? '男' : value === 'female' ? '女' : value)}</b></div>`).join('') || '<p class="muted">等待补充健康信息</p>';
  $('#profile-status').textContent = data.missing?.length ? `还需 ${data.missing.length} 项信息` : '评估资料已齐全';
  $('#memory-note').hidden = !data.from_memory?.length;
  $('#memory-note').textContent = '来自长期记忆：' + (data.from_memory || []).map(key => labels[key] || key).join('、');
}
function showRisk(risk) {
  if (!risk || risk.risk_percent === undefined) return;
  $('#risk-card').className = `risk-card ${risk.level}`;
  $('#risk-value').textContent = `${(Math.round((risk.risk_percent + Number.EPSILON) * 10)/10).toFixed(1)}%`;
  $('#risk-level').textContent = `${({low:'低',intermediate:'中',high:'高'})[risk.level] || ''}风险`;
  $('#risk-caption').textContent = 'Framingham 模型估算 · 仅供演示';
}
function paragraph(parent, text, className='') { const p=document.createElement('p'); p.textContent=text; p.className=className; parent.append(p); }
function jsonDetails(parent, title, value) {
  const details=document.createElement('details'); const summary=document.createElement('summary'); summary.textContent=title;
  const pre=document.createElement('pre'); pre.textContent=JSON.stringify(value,null,2); details.append(summary,pre); parent.append(details);
}
function addNode(event, turnId) {
  const data=event.data || {}, [owner,color]=agents[event.agent] || [event.agent,'#80988b'];
  const card=document.createElement('article'); card.className='trace-card'; card.style.setProperty('--agent-color',color);
  card.innerHTML=`<div class="trace-title"><strong>${escapeHTML(nodes[event.node] || event.node)}</strong><span class="trace-time">${Number(event.ms)} ms</span></div><span class="agent-tag">${escapeHTML(owner)}</span>`;
  if (event.node === 'intake') { showProfile(data); paragraph(card, data.missing?.length ? `还需补充：${data.missing.map(k=>labels[k] || k).join('、')}` : '必填资料已齐全'); if(data.from_memory?.length) paragraph(card,'来自长期记忆：'+data.from_memory.map(k=>labels[k] || k).join('、'),'success'); }
  if (event.node === 'safety_check') paragraph(card,data.red_flags?.length ? '发现急症信号，立即进入紧急指引。' : '未命中急症关键词，继续问诊。',data.red_flags?.length ? 'warning' : 'success');
  if (event.node === 'supervisor') paragraph(card,`${agents[data.route]?.[0] || data.route} · ${data.reason}`);
  if (event.node === 'verify') paragraph(card,data.ok ? `引用校验通过 · 第 ${data.attempt} 次` : (data.problems || []).join('；'),data.ok ? 'success' : 'warning');
  if (data.followup) paragraph(card,data.followup);
  if (data.degraded) paragraph(card,'模型暂不可用，已降级为本地模板。','warning');
  if (data.fallback) paragraph(card,'生成内容未通过校验，已使用安全模板。','warning');
  if (data.prompt_version) paragraph(card,`提示词版本 ${data.prompt_version}`);
  for (const call of data.tool_calls || []) {
    jsonDetails(card,`${call.tool} · MCP${call.cached ? ' · 缓存命中' : ''}`,{参数:call.args,结果:call.result});
    if (call.tool === 'cvd_risk' && !call.result.error) showRisk(call.result);
  }
  for (const chunk of data.chunks || []) {
    const source=document.createElement('div'); source.className='source-card'; source.tabIndex=-1;
    const title=document.createElement('b'); title.textContent=chunk.title; source.append(title);
    paragraph(source,chunk.source); paragraph(source,`相关度 ${Number(chunk.score).toFixed(3)}`);
    card.append(source); sourceElements.set(`${turnId}:${chunk.id}`,source);
  }
  if (event.node === 'checkin') paragraph(card,`风险变化：${data.delta > 0 ? '+' : ''}${data.delta} 个百分点`);
  $('#traces').append(card); $('#trace-count').textContent=++nodeCount; $('#traces').scrollTop=$('#traces').scrollHeight;
  $('#chat-status').textContent=nodes[event.node] || '正在处理…';
}
function setTraceVisible(visible) { $('#trace-panel').hidden=!visible; $('#workspace').classList.toggle('trace-hidden',!visible); $('#toggle-trace').setAttribute('aria-expanded',String(visible)); $('#toggle-trace').textContent=visible ? '收起轨迹 ↗' : '展开轨迹 ↙'; }
$('#toggle-trace').addEventListener('click',()=>setTraceVisible($('#trace-panel').hidden));
$('#messages').addEventListener('click',event=>{
  const button=event.target.closest('[data-source]'); if(!button) return;
  setTraceVisible(true); const source=sourceElements.get(button.dataset.source);
  if(source) { document.querySelectorAll('.source-card.highlight').forEach(el=>el.classList.remove('highlight')); source.classList.add('highlight'); source.scrollIntoView({behavior:'smooth',block:'center'}); source.focus({preventScroll:true}); }
});
function setBusy(value) { busy=value; $('#send').disabled=value; $('#new-session').disabled=value; $('#clear-identity').disabled=value; document.querySelectorAll('[data-example]').forEach(b=>b.disabled=value); $('#send').innerHTML=value ? '处理中…' : '发送 ↑'; }
async function send(text) {
  if(busy || !text.trim()) return;
  setBusy(true); turn++; const currentTurn=turn; addMessage(text,'user',currentTurn); $('#message-input').value='';
  $('.trace-empty')?.remove(); const marker=document.createElement('div'); marker.className='turn-marker'; marker.textContent=`第 ${turn} 轮对话`; $('#traces').append(marker);
  $('#chat-status').textContent='正在连接健康助手…'; let gotDone=false, gotMessage=false, gotError=false;
  try {
    const response=await fetch('/api/chat',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({thread_id:threadId,user_id:userId,message:text})});
    if(!response.ok || !response.body) throw new Error('连接暂不可用，请稍后重试。');
    const reader=response.body.getReader(), decoder=new TextDecoder(); let buffer='';
    function consume(block) {
      const lines=block.split('\n').filter(line=>line.startsWith('data:')).map(line=>line.slice(5).trimStart()); if(!lines.length) return;
      const event=JSON.parse(lines.join('\n'));
      if(event.type==='node') addNode(event,currentTurn);
      else if(event.type==='message') { gotMessage=true; addMessage(event.content,'assistant',currentTurn); }
      else if(event.type==='error') { gotError=true; addMessage(event.message,'error',currentTurn); }
      else if(event.type==='done') gotDone=true;
    }
    while(true) {
      const {value,done}=await reader.read(); buffer+=decoder.decode(value || new Uint8Array(),{stream:!done}).replace(/\r\n/g,'\n');
      let boundary; while((boundary=buffer.indexOf('\n\n'))>=0) { consume(buffer.slice(0,boundary)); buffer=buffer.slice(boundary+2); }
      if(done) { if(buffer.trim()) consume(buffer); break; }
    }
    if(!gotDone || (!gotMessage && !gotError)) throw new Error('连接中断，回复未完成。请重试。');
    $('#chat-status').textContent=gotError ? '本轮未完成，请检查信息后重试' : '本轮已完成 · 点击引用查看来源';
  } catch(error) { addMessage(error.message || '网络异常，请稍后重试。','error',currentTurn); $('#chat-status').textContent='连接未完成'; }
  finally { setBusy(false); $('#message-input').focus(); }
}
$('#chat-form').addEventListener('submit',event=>{event.preventDefault(); send($('#message-input').value.trim());});
$('#message-input').addEventListener('keydown',event=>{if(event.key==='Enter' && !event.shiftKey && !event.isComposing) {event.preventDefault(); send(event.target.value.trim());}});
document.querySelectorAll('[data-example]').forEach(button=>button.addEventListener('click',()=>send(button.dataset.example)));
function resetSession() {
  threadId=createId(); turn=0; nodeCount=0; sourceIndex=0; sourceElements.clear(); sourceNumbers.clear();
  $('#messages').replaceChildren(); $('#traces').replaceChildren(); $('#trace-count').textContent='0'; $('#message-input').value='';
  $('#profile').innerHTML='<p class="muted">新会话将在下一条消息时读取已有健康档案。</p>'; $('#profile-status').textContent='等待对话'; $('#memory-note').hidden=true;
  $('#risk-card').className='risk-card'; $('#risk-value').textContent='—'; $('#risk-level').textContent='还未评估'; $('#risk-caption').textContent='完善资料后查看风险分层';
  $('#chat-status').textContent='已开启新会话'; welcome();
}
$('#new-session').addEventListener('click',resetSession);
$('#clear-identity').addEventListener('click',()=>{userId=createId(); try{localStorage.setItem('healthpilot-user',userId);}catch{} resetSession(); $('#profile').innerHTML='<p class="muted">已切换为新身份，请重新提供健康信息。</p>'; $('#chat-status').textContent='已更换身份（服务端历史不会删除）';});
fetch('/api/health').then(r=>{if(!r.ok) throw new Error(); return r.json();}).then(data=>{$('#mode').textContent=data.mock ? '● Mock 演示模式' : '● 千问真实模式';}).catch(()=>{$('#mode').textContent='服务暂不可用';});
welcome();
