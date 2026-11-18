const $=s=>document.querySelector(s);
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let TOKEN=sessionStorage.getItem('ila_admin')||'';
async function api(p,m='GET',b){
  const h={'Content-Type':'application/json','X-ILA-Client':'1'};if(TOKEN)h['x-admin-token']=TOKEN;
  const r=await fetch('/api'+p,{method:m,headers:h,body:b===undefined?undefined:JSON.stringify(b)});
  if(r.status===401){const t=prompt('Admin token required');if(t){TOKEN=t;sessionStorage.setItem('ila_admin',t);return api(p,m,b)}}
  const j=await r.json().catch(()=>({}));
  if(!r.ok)throw new Error(j.detail||j.error||r.statusText);return j;
}
function toast(t){const e=$('#toast');e.textContent=t;e.classList.add('on');setTimeout(()=>e.classList.remove('on'),t.length>60?7000:2600)}
const act=async(f)=>{try{await f()}catch(e){toast(e.message)}await render()};
const fmt=t=>t?new Date(t*1000).toLocaleString('en-US',{timeZone:ST&&ST.profile?ST.profile.timezone:undefined,month:'short',day:'numeric',hour:'numeric',minute:'2-digit'}):'-';
const who=l=>((l.first_name||'')+' '+(l.last_name||'')).trim()||l.email||'(no name)';
const nm=l=>esc(((l.first_name||'')+' '+(l.last_name||'')).trim()||l.email);
const tier=t=>`<span class="pill ${t}">${esc(String(t||'').replace('_',' '))}</span>`;
const st=s=>`<span class="pill ${s}">${esc(String(s||'').replace(/_/g,' '))}</span>`;
let ST=null,ROUTE='overview',PARAM=null;
const NAV=[['overview','Overview'],['leads','Leads'],['review','Review queue'],['handoffs','Handoffs'],['followups','Follow-ups'],['setup','Setup chat'],['settings','Settings'],['demo','Demo lab']];
function go(r,p){ROUTE=r;PARAM=p||null;location.hash=r+(p?'/'+p:'');$('#side').classList.remove('open');render()}
window.addEventListener('hashchange',()=>{const [r,p]=location.hash.slice(1).split('/');if(r&&(r!==ROUTE||p!==PARAM)){ROUTE=r;PARAM=p||null;render()}});
$('#menu').onclick=()=>$('#side').classList.toggle('open');
$('#kill').onclick=()=>act(async()=>{await api('/kill-switch','POST',{on:!ST.kill_switch});toast(ST.kill_switch?'Sending resumed':'All sending paused')});
async function shell(){
  ST=await api('/status');
  $('#nav').innerHTML=NAV.map(([k,l])=>{let n='';if(k==='review'&&ST.pending_review)n=ST.pending_review;if(k==='handoffs'&&ST.open_handoffs)n=ST.open_handoffs;
    return `<a href="#${k}" class="${ROUTE===k?'on':''}" ${ROUTE===k?'aria-current="page"':''} onclick="event.preventDefault();go('${k}')"><span>${l}</span>${n?`<span class="n">${n}</span>`:''}</a>`}).join('');
  $('#title').textContent=(NAV.find(x=>x[0]===ROUTE)||['','Lead'])[1];
  const i=ST.integrations;
  $('#pills').innerHTML=`<span class="pill ${i.channel.startsWith('sim')?'demo':'real'}">Channel: ${esc(i.channel)}</span> <span class="pill ${i.crm.startsWith('sim')?'demo':'real'}">CRM: ${esc(i.crm)}</span> <span class="pill">${i.model.startsWith('offline')?'Offline rules':'Model configured'}</span>`;
  $('#clockbox').innerHTML=ST.clock.simulated?`Simulated clock<br><b style="color:#fff">${fmt(ST.clock.now)}</b><br>${esc(ST.profile.timezone)}`:`Real clock<br><b style="color:#fff">${fmt(ST.clock.now)}</b>`;
  const k=$('#kill');k.textContent=ST.kill_switch?'Sending PAUSED. Resume':'Pause all sending';k.classList.toggle('on',ST.kill_switch);
  let b='';
  if(ST.kill_switch)b+='<div class="b red"><b>Kill switch is on.</b> Nothing will be sent until you resume.</div>';
  const nLeads=Object.values(ST.lead_status_counts||{}).reduce((a,c)=>a+c,0);
  if(i.channel.startsWith('sim')&&!ST.profile_configured&&nLeads===0)b+='<div class="b"><b>NOT SET UP YET.</b> Nothing real can be sent in this local build. Start with <a href="#setup" onclick="event.preventDefault();go(\'setup\')">Setup chat</a> to describe your business, or load the fictional scenario in <a href="#demo" onclick="event.preventDefault();go(\'demo\')">Demo lab</a>.</div>';
  else if(i.channel.startsWith('sim'))b+='<div class="b"><b>DEMO MODE.</b> Fictional company and leads. No real email, WhatsApp or CRM call can happen. Sent messages appear only in this app.</div>';
  $('#banner').innerHTML=b;
}
const fixLabels=()=>document.querySelectorAll('#view label:not([for])').forEach(l=>{const n=l.nextElementSibling;if(n&&n.id&&/^(INPUT|SELECT|TEXTAREA)$/.test(n.tagName))l.setAttribute('for',n.id)});
async function render(){
  await shell();const v=$('#view');
  try{
    if(ROUTE==='overview')await overview(v);else if(ROUTE==='leads')PARAM?await lead(v,PARAM):await leads(v);
    else if(ROUTE==='review')await review(v);else if(ROUTE==='handoffs')await handoffs(v);else if(ROUTE==='followups')await followups(v);
    else if(ROUTE==='setup')await setup(v);else if(ROUTE==='settings')await settings(v);else if(ROUTE==='demo')await demo(v);
  }catch(e){v.innerHTML=`<div class="card" role="alert">Error: ${esc(e.message)}</div>`}
  fixLabels();
}
async function overview(v){
  const m=await api('/metrics');const M=m.metrics;const t=M.tier_distribution.counts;
  const kp=(k,l)=>`<div class="card kpi"><div class="l">${l}</div><div class="v">${esc(M[k].display.split(' (')[0].replace(/ \(.*/,'').replace(/ \(too few.*/,''))}</div><div class="d">${esc(M[k].display.includes('(')?'('+M[k].display.split(' (').slice(1).join(' (').replace(/\)$/,'')+')':'')} ${esc(M[k].note||'')}</div></div>`;
  const total=Object.values(t).reduce((a,b)=>a+b,0)||1,col={hot:'#e11d48',warm:'#f59e0b',nurture:'#3b82f6',not_fit:'#98a2b3',unknown:'#a855f7'};
  const rows=Object.entries(M).map(([k,x])=>`<tr><td><b>${k.replace(/_/g,' ')}</b></td><td>${esc(x.display)}</td><td class="mut sm">${esc(x.definition)}<br>Source: ${esc(x.source)}</td></tr>`).join('');
  v.innerHTML=`<div class="grid g4">${kp('leads_received','Leads received')}${kp('first_response_time','Median first response')}${kp('reply_rate','Reply rate')}
   <div class="card kpi"><div class="l">Needs your attention</div><div class="v">${ST.pending_review+ST.open_handoffs}</div><div class="d">${ST.pending_review} drafts to review, ${ST.open_handoffs} open handoffs</div></div></div>
   <div class="grid g3" style="margin-top:16px"><div class="card"><h3>Lead tiers<small>current, after overrides</small></h3>
   <div class="bar">${Object.entries(t).map(([k,n])=>`<i style="width:${n/total*100}%;background:${col[k]}"></i>`).join('')}</div>
   <div class="row" style="margin-top:10px">${Object.entries(t).map(([k,n])=>`<span>${tier(k)} ${n}</span>`).join('')}</div>
   <h3 style="margin-top:20px">Status</h3><div class="row">${Object.entries(ST.lead_status_counts).map(([k,n])=>`<span>${st(k)} ${n}</span>`).join('')||'<span class="mut">No leads yet. Open Demo lab to load the fictional scenario.</span>'}</div></div>
   <div class="card"><h3>Not measured</h3><p class="sm mut">Meetings booked, wins and revenue are not measured. They need a CRM outcome feed that this build does not have. Percentages appear only when at least 20 leads back them.</p></div></div>
   <div class="card tbl" style="margin-top:16px"><h3>Every metric, with its definition</h3><table class="mt"><tr><th>Metric</th><th>Value</th><th>Definition and source</th></tr>${rows}</table></div>`;
}
async function leads(v){
  const all=await api('/leads');const f=window._lf||{q:'',tier:'',status:''};
  const rows=all.filter(l=>(!f.tier||l.effective_tier===f.tier)&&(!f.status||l.status===f.status)&&(!f.q||(nm(l)+l.company+l.email).toLowerCase().includes(f.q.toLowerCase())));
  v.innerHTML=`<div class="filters"><input id="q" aria-label="Search leads" placeholder="Search name, company, email" value="${esc(f.q)}"><select id="ft" aria-label="Filter by tier"><option value="">All tiers</option>${['hot','warm','nurture','not_fit','unknown'].map(x=>`<option ${f.tier===x?'selected':''}>${x}</option>`).join('')}</select>
   <select id="fs" aria-label="Filter by status"><option value="">All statuses</option>${[...new Set(all.map(l=>l.status))].map(x=>`<option ${f.status===x?'selected':''}>${x}</option>`).join('')}</select>
   <span class="sp"></span><button class="btn pri" onclick="newLead()">Add test lead</button></div>
   <div class="card tbl" style="padding:6px 8px"><table class="lt"><tr><th>Lead</th><th class="co">Company</th><th>Tier</th><th>Status</th><th>Owner</th><th>Flags</th></tr>
   ${rows.map(l=>`<tr class="click" tabindex="0" role="link" onkeydown="if(event.key==='Enter')this.click()" onclick="go('leads',${l.id})"><td><b>${nm(l)}</b><div class="mut sm">${esc(l.job_title||'')}</div><div class="mut sm mco">${esc(l.company||'')}</div></td><td class="co">${esc(l.company||'-')}</td><td>${tier(l.effective_tier)}${(l.flags||[]).includes('size_conflict')?' <span class="pill warm">provisional</span>':''}</td><td>${st(l.status)}${l.handoff_status?' '+st(l.handoff_status):''}</td><td class="sm">${esc(l.owner||'-')}</td><td class="sm">${(l.flags||[]).map(esc).join(', ')}</td></tr>`).join('')||`<tr><td colspan=6 class="empty">${all.length?'No leads match these filters.':'No leads yet. Next step: click Add test lead, or load the fictional scenario in Demo lab.'}</td></tr>`}</table></div>`;
  const u=()=>{window._lf={q:$('#q').value,tier:$('#ft').value,status:$('#fs').value};render()};
  $('#ft').onchange=u;$('#fs').onchange=u;$('#q').onchange=u;
}
window.newLead=()=>{setTimeout(fixLabels,0);$('#view').innerHTML=`<div class="card" style="max-width:640px"><h3>Add a test lead</h3><p class="sm mut">Goes through the same intake as a web form. Use fictional data.</p>
 ${[['first_name','First name'],['last_name','Last name'],['email','Work email'],['company','Company'],['job_title','Job title'],['company_size','Company size (number or range)']].map(([k,l])=>`<label>${l}</label><input id="n_${k}">`).join('')}
 <label>Message</label><textarea id="n_message"></textarea><label><input type="checkbox" id="n_consent" style="width:auto"> Contact consent was given on the form</label>
 <div class="row" style="margin-top:14px"><button class="btn pri" onclick="saveLead()">Submit</button><button class="btn" onclick="render()">Cancel</button></div></div>`};
window.saveLead=()=>act(async()=>{const b={inquiry_type:'info'};['first_name','last_name','email','company','job_title','company_size','message'].forEach(k=>b[k]=$('#n_'+k).value);b.consent=$('#n_consent').checked;b.consent_source='manual test lead form';
  const r=await api('/intake/form','POST',b);toast(r.duplicate?'Duplicate ignored':(r.error||'Lead created'));if(r.lead_id)go('leads',r.lead_id)});
async function lead(v,id){
  const d=await api('/leads/'+id);const L=d.lead;
  const fl=d.fields.filter(f=>!f.superseded);
  v.innerHTML=`<div class="row" style="margin-bottom:14px"><button class="btn" onclick="go('leads')">&larr; Leads</button><h2 style="margin:0">${nm(L)}</h2>${tier(L.effective_tier)}${(L.flags||[]).includes('size_conflict')?'<span class="pill warm">provisional</span>':''}${st(L.status)}<span class="sp"></span></div>
  <div class="grid g3"><div>
   <div class="card"><h3>Conversation</h3>${d.messages.map(m=>`<div class="msg ${m.direction==='outbound'?(m.author&&m.author!=='agent'?'human':'out'):''}"><div class="h">${esc(m.direction)} &middot; ${esc(m.author||m.channel||'')} &middot; ${fmt(m.created_at)}</div>${esc(m.subject?m.subject+'\n\n':'')}${esc(m.body)}</div>`).join('')||'<div class="empty">No messages</div>'}
    ${L.status==='opted_out'||L.status==='no_consent'?'':`<label>Simulate the lead replying</label><textarea id="rt" placeholder="Type what the lead would write back"></textarea><div class="row" style="margin-top:8px"><button class="btn pri" onclick="simReply(${id})">Send as lead</button><button class="btn" onclick="humanReply(${id})">Record a human message</button></div>`}</div>
   ${d.actions.length?`<div class="card" style="margin-top:16px"><h3>Drafts and sends</h3>${d.actions.map(a=>`<div class="fact"><b>${esc(a.kind)}</b> ${st(a.status)} ${a.send_state?`<span class="pill">${esc(a.send_state)}</span>`:''}</div>`).join('')}</div>`:''}
   <div class="card" style="margin-top:16px"><h3>Audit trail</h3><div class="tl">${d.audit.map(a=>`<div><span>${fmt(a.ts)}</span><b>${esc(a.event)}</b> ${esc(typeof a.detail==='object'?JSON.stringify(a.detail):a.detail||'')}</div>`).join('')}</div></div></div>
  <div>
   <div class="card"><h3>Lead</h3><div class="sm">${esc(L.email)}<br>${esc(L.phone||'no phone')}<br>${esc(L.job_title||'')}, ${esc(L.company||'')}<br>Owner: ${esc(L.owner||'-')}<br>Consent: ${L.consent?'yes, '+esc(L.consent_source||''):'<b>not given</b>'}<br>Questions asked: ${L.questions_asked}, touches: ${L.touches}</div>
    ${L.status==='declined_interest'?`<div class="note" style="margin-top:8px">This person said they are not interested. <b>No reply and no follow-ups were sent.</b> You can record a human message if you want to reach out.</div>`:''}${L.status==='closed_not_fit'?`<div class="note" style="margin-top:8px">Closed as not a fit. ${d.actions.some(a=>a.kind==='decline')?'A decline draft exists (see Drafts).':'<b>No message was sent to this person.</b>'} To reopen, override the tier below with a reason.</div>`:''}${(L.flags||[]).includes('size_conflict')?'<div class="note" style="margin-top:8px">Company size sources disagree. Neither number is treated as verified and the tier is provisional until a human confirms the size.</div>':''}${(L.flags||[]).includes('invalid_timezone')?'<div class="note" style="margin-top:8px">The time zone this lead supplied was not recognised. Your own time zone is used for sending hours.</div>':''}${(L.flags||[]).includes('conditional_refusal')?'<div class="note" style="margin-top:8px">Conditional refusal. Read the message above, including any referral. Nothing was sent.</div>':''}${(L.flags||[]).includes('possible_opt_out')?'<div class="note" style="margin-top:8px">Wording may be a request to stop. Nothing is sent until a human decides.</div>':''}${L.paused_human?'<div class="note" style="margin-top:8px">A human is handling this lead. The agent is paused.</div>':''}</div>
   <div class="card" style="margin-top:16px"><h3>Why this tier</h3><ul class="sm" style="padding-left:18px;margin:0">${(L.tier_reasons||[]).map(r=>`<li>${esc(r)}</li>`).join('')}</ul>
    ${(L.missing||[]).length?`<p class="sm mut">Still unknown: ${L.missing.map(esc).join(', ')}</p>`:''}
    <label>Override tier (reason required)</label><div class="row"><select id="ot" style="width:auto">${['hot','warm','nurture','not_fit'].map(x=>`<option>${x}</option>`).join('')}</select><input id="or" placeholder="Reason"><button class="btn sm" onclick="over(${id})">Apply</button></div></div>
   <div class="card" style="margin-top:16px"><h3>Facts and evidence</h3>${fl.map(f=>`<div class="fact"><b>${esc(f.name)}</b> ${esc(f.value)} <span class="pill">${esc(f.source)}</span><q>${esc(f.evidence)}</q></div>`).join('')||'<span class="mut">No facts yet</span>'}</div>
   ${d.handoff?`<div class="card" style="margin-top:16px"><h3>Handoff ${st(d.handoff.status)}</h3><p class="sm">${esc(d.handoff.card.why_handed_off)}</p>${d.handoff.status!=='acknowledged'?`<button class="btn sm pri" onclick="ack(${id})">Acknowledge as owner</button>`:''}</div>`:''}</div></div>`;
}
window.simReply=id=>act(async()=>{const t=$('#rt').value.trim();if(!t)return toast('Type a reply first');await api(`/leads/${id}/reply`,'POST',{text:t});toast('Reply processed')});
window.humanReply=id=>act(async()=>{const t=$('#rt').value.trim();if(!t)return toast('Type the message you sent first');await api(`/leads/${id}/human-reply`,'POST',{text:t,author:'Sales rep'});toast('Human takeover recorded, agent paused')});
window.over=id=>act(async()=>{await api(`/leads/${id}/override`,'POST',{tier:$('#ot').value,reason:$('#or').value});toast('Override saved')});
window.ack=id=>act(async()=>{await api(`/handoffs/${id}/ack`,'POST');toast('Acknowledged')});
async function review(v){
  const a=await api('/actions');const u=await api('/actions?status=uncertain');
  const unc=u.map(x=>`<div class="card" style="margin-bottom:14px;border-color:#f3c5c0"><div class="row"><b>${esc(who(x))}</b><span class="mut">${esc(x.company||'')}</span><span class="pill escalated">send unverified</span><span class="sp"></span><span class="mut sm">To: ${esc(x.email)}</span></div>
   <p class="sm">The channel timed out and cannot confirm whether this message was delivered. Nothing will be resent automatically. Check the channel yourself, then choose:</p><div class="note" style="margin-bottom:8px">${esc(x.subject)}<br>${esc((x.body||'').slice(0,160))}...</div>
   <div class="row"><button class="btn sm pri" onclick="resolveU(${x.id},'confirmed_sent')">It was delivered</button><button class="btn sm" onclick="resolveU(${x.id},'resend')">Resend it</button><button class="btn sm" onclick="resolveU(${x.id},'drop')">Drop it</button></div></div>`).join('');
  v.innerHTML=unc+(a.length?a.map(x=>`<div class="card" style="margin-bottom:14px"><div class="row"><b>${esc(who(x))}</b><span class="mut">${esc(x.company||'')}</span>${tier(x.tier)}<span class="pill">${esc(x.kind)}</span><span class="sp"></span><span class="mut sm">To: ${esc(x.email)}</span></div>
   <label>Subject</label><div><b>${esc(x.subject)}</b></div><label>Message (edit before approving if needed)</label><textarea id="b${x.id}" style="min-height:170px">${esc(x.body)}</textarea>
   <div class="row" style="margin-top:10px"><button class="btn pri" onclick="appr(${x.id})">Approve and send</button><button class="btn" onclick="rej(${x.id})">Reject</button><span class="mut sm">Guards (hours, consent, pause) are checked again at send time.</span></div></div>`).join(''):(unc?'':'<div class="card empty">Nothing waiting for review. Drafts appear here when a lead is created or replies. Add a test lead on the Leads page to see one.</div>'));
}
window.resolveU=(id,r)=>act(async()=>{await api(`/actions/${id}/resolve`,'POST',{resolution:r});toast('Recorded: '+r)});
window.appr=id=>act(async()=>{const r=await api(`/actions/${id}/approve`,'POST',{body:$('#b'+id).value});toast(r.status==='sent'?'Sent (simulated)':r.status==='deferred'?(/postal address/.test(r.reason||'')?'Not sent yet: set the sender postal address in Settings, then approve again.':'Held: '+(r.reason||'waiting')+(r.until?'. It will go out at the next allowed time.':'')):'Result: '+(r.status||r.error||'queued'))});
window.rej=id=>act(async()=>{await api(`/actions/${id}/reject`,'POST',{reason:'rejected in review'});toast('Rejected')});
async function handoffs(v){
  const h=await api('/handoffs');
  v.innerHTML=h.length?h.map(x=>`<div class="card" style="margin-bottom:14px"><div class="row"><b>${esc(x.card.name)}</b><span class="mut">${esc(x.card.company||'')}, ${esc(x.card.job_title||'')}</span>${tier(x.card.tier)}${st(x.status)}<span class="pill">${esc(x.card.kind)}</span><span class="sp"></span>
   <span class="mut sm">Owner: ${esc(x.owner||'-')} &middot; SLA due ${fmt(x.sla_due)}</span></div>
   <p>${esc(x.card.why_handed_off)}</p>${(x.card.flags||[]).length?`<div class="note">Flags: ${x.card.flags.map(esc).join(', ')}</div>`:''}
   <div class="sm"><b>Suggested opening:</b> ${esc(x.card.suggested_opening)}<br><b>Still unknown:</b> ${(x.card.unknown||[]).map(esc).join(', ')||'nothing'}</div>
   <div class="row" style="margin-top:10px"><button class="btn sm" onclick="go('leads',${x.lead_id})">Open lead</button>${x.status!=='acknowledged'?`<button class="btn sm pri" onclick="ack(${x.lead_id})">Acknowledge</button>`:''}</div></div>`).join(''):'<div class="card empty">No handoffs yet. A lead lands here when it is a strong fit or needs a person. Load the demo in Demo lab to see examples.</div>';
}
async function followups(v){
  const f=await api('/followups');
  v.innerHTML=`<div class="card tbl"><table class="mt"><tr><th>Lead</th><th>Step</th><th>Due</th><th>Status</th><th>Reason</th></tr>${f.map(x=>`<tr class="click" tabindex="0" role="link" onkeydown="if(event.key==='Enter')this.click()" onclick="go('leads',${x.lead_id})"><td>${esc(who(x))}<div class="mut sm">${esc(x.company||'')}</div></td><td data-l="Step">${x.step}</td><td data-l="Due">${fmt(x.due_at)}</td><td data-l="Status">${st(x.status)}</td><td class="sm" data-l="Reason">${esc(x.cancel_reason||'')}</td></tr>`).join('')||'<tr><td colspan=5 class="empty">No follow-ups scheduled.</td></tr>'}</table></div>
  <p class="sm mut">Follow-ups stop when a lead replies, opts out, is handed to a human, or reaches the touch limit. Each one still passes the sending-hours and consent guards.</p>`;
}
async function setup(v){
  const c=await api('/chat');
  v.innerHTML=`<div class="card" style="max-width:760px"><div class="chat" id="ch">${c.messages.map(m=>`<div class="bub ${m.role}">${esc(m.text)}</div>`).join('')}</div>
  <div class="row" style="margin-top:12px"><input id="ci" placeholder="Type your answer, or 'skip' to keep the default"><button class="btn pri" id="cs">Send</button><button class="btn" id="cr">Restart</button></div>
  <p class="sm mut">This assistant is a fixed question flow that runs offline. Never paste API keys or passwords. Credentials are set as environment variables, see Settings.</p></div>`;
  const ch=$('#ch');ch.scrollTop=ch.scrollHeight;const send=()=>act(async()=>{const t=$('#ci').value;if(!t.trim())return;await api('/chat','POST',{text:t})});
  $('#cs').onclick=send;$('#ci').onkeydown=e=>{if(e.key==='Enter')send()};$('#cr').onclick=()=>act(()=>api('/chat/reset','POST'));
}
async function settings(v){
  const p=await api('/profile');const sup=await api('/suppression');const i=ST.integrations;
  const yes=b=>b?'<span class="pill ok">yes</span>':'<span class="pill">no</span>';
  v.innerHTML=`<div class="grid g2 stack"><div class="card"><h3>Integrations</h3>
   <div class="fact"><b>Sending channel</b> ${esc(i.channel)}</div><div class="fact"><b>CRM</b> ${esc(i.crm)}</div>
   <div class="fact"><b>Model</b> ${esc(i.model)}</div><div class="fact"><b>GHL</b> <span class="sm">${esc(i.ghl_status)}</span></div>
   <div class="fact"><b>GHL credentials set</b> ${yes(i.ghl_credentials_present)} <b style="min-width:0">Live GHL on</b> ${yes(i.ghl_live_enabled)}</div>
   <div class="fact"><b>Model key set</b> ${yes(i.model_key_present)}</div><div class="fact"><b>Admin token</b> ${yes(i.admin_token_set)} <b style="min-width:0">Intake secret</b> ${yes(i.intake_secret_set)}</div>
   <div class="note" style="margin-top:12px">Credentials are bring-your-own and read from environment variables when the server starts. This page shows only whether each is present. It never accepts or displays a secret. See the README for the variable names.</div></div>
  <div class="card"><h3>Agent profile</h3>
   ${p.configured?'':'<div class="note">Not set up yet. The fastest way is the <a href="#setup" onclick="event.preventDefault();go(\'setup\')">Setup chat</a>. You can also fill this in and save.</div>'}
   <label>Company</label><input id="p_company" value="${p.configured?esc(p.company):''}" placeholder="Your company name"><label>Offer</label><textarea id="p_offer" rows="2" placeholder="What you sell, in one line">${p.configured?esc(p.offer):''}</textarea>
   <label>Sender identity (signature on every email)</label><input id="p_sig" value="${p.configured?esc(p.signature):''}" placeholder="Name, role, company">
   <label>Postal address for the email footer (required before real sending)</label><input id="p_addr" value="${esc(p.postal_address||'')}" placeholder="Street, city, postal code, country">
   <div class="row"><div class="sp"><label>Minimum employees</label><input id="p_min" type="number" value="${p.min_employees}"></div><div class="sp"><label>Time zone (IANA)</label><input id="p_tz" value="${esc(p.timezone)}"></div></div>
   <div class="row"><div class="sp"><label>No sending from (hour)</label><input id="p_qs" type="number" value="${p.quiet_start_hour}"></div><div class="sp"><label>Until (hour)</label><input id="p_qe" type="number" value="${p.quiet_end_hour}"></div></div>
   <div class="row"><div class="sp"><label>Handoff owner</label><input id="p_own" value="${esc(p.handoff_owner)}"></div><div class="sp"><label>SLA (minutes)</label><input id="p_sla" type="number" value="${p.handoff_sla_minutes}"></div></div>
   <label>Assumed country code for phone numbers without + (blank = never guess)</label><input id="p_cc" value="${esc(p.default_cc)}">
   <label>When a lead is not a fit</label><select id="p_dp"><option value="size_only" ${p.decline_policy==='size_only'?'selected':''}>Draft a polite decline for size only (you approve it). Other closes send nothing</option><option value="none" ${p.decline_policy==='none'?'selected':''}>Never draft a decline. Close silently</option></select>
   <label>Mode</label><select id="p_mode"><option value="approve" ${p.mode==='approve'?'selected':''}>Approve every message</option><option value="auto_followups" ${p.mode==='auto_followups'?'selected':''}>Approve first message, auto follow-ups</option></select>
   <div class="row" style="margin-top:12px"><button class="btn pri" onclick="saveP()">Save profile</button></div>
   <p class="sm mut">Sending hours use the operator's own time zone. Each operator must have their local rules (consent, marketing and messaging law) reviewed by qualified counsel. This software gives no legal assurance.</p><p class="sm mut">Not provided in this build: a consent-record export and an erase-on-request tool for leads. Operators must handle access and deletion requests themselves, for example in their CRM, before real use.</p></div></div>
  <div class="card tbl" style="margin-top:16px"><h3>Suppression list<small>never contacted again</small></h3><table><tr><th>Type</th><th>Key</th><th>Reason</th><th>Added</th></tr>${sup.map(s=>`<tr><td>${esc(s.kind)}</td><td>${esc(s.key)}</td><td>${esc(s.reason)}</td><td>${fmt(s.created_at)}</td></tr>`).join('')||'<tr><td colspan=4 class="empty">Empty</td></tr>'}</table></div>`;
}
window.saveP=()=>act(async()=>{await api('/profile','PUT',{company:$('#p_company').value,offer:$('#p_offer').value,signature:$('#p_sig').value,postal_address:$('#p_addr').value,min_employees:+$('#p_min').value,timezone:$('#p_tz').value,quiet_start_hour:+$('#p_qs').value,quiet_end_hour:+$('#p_qe').value,handoff_owner:$('#p_own').value,handoff_sla_minutes:+$('#p_sla').value,mode:$('#p_mode').value,default_cc:$('#p_cc').value.trim(),decline_policy:$('#p_dp').value});toast('Profile saved')});
async function demo(v){
  const sim=ST.clock.simulated;
  v.innerHTML=`<div class="grid g2 stack"><div class="card"><h3>Fictional scenario</h3><p class="sm">Loads about 24 invented B2B leads from a made-up company, "Brightlane", and plays them through the real pipeline on a simulated clock: a hot VP, a tiny company, a competitor, a job seeker, a vendor pitch, a disposable email, a missing consent box, a STOP reply, a prompt-injection attempt, a company size conflict, a human takeover, and follow-ups.</p>
   <button class="btn pri" onclick="act(async()=>{await api('/demo/load','POST');toast('Demo loaded');go('overview')})">Load demo scenario</button> <button class="btn" onclick="if(!confirm('Delete all leads, messages, drafts and your saved setup from this local workspace? This cannot be undone.'))return;act(async()=>{await api('/demo/reset','POST');toast('Workspace emptied');go('setup')})">Empty workspace</button></div>
   <div class="card"><h3>Simulated clock</h3>${sim?`<p class="sm">Now: <b>${fmt(ST.clock.now)}</b> (${esc(ST.profile.timezone)}). Advancing runs the scheduler once.</p><div class="row">${[1,8,24,72].map(h=>`<button class="btn" onclick="adv(${h})">+${h}h</button>`).join('')}<button class="btn" onclick="act(async()=>{const r=await api('/tick','POST');toast('Tick: '+JSON.stringify(r))})">Run scheduler</button></div>`:'<p class="sm mut">The clock is real. Load the demo to use a simulated one.</p>'}</div>
   <div class="card"><h3>Failure injection</h3><p class="sm">Force the simulated channel to misbehave, then approve a draft and watch the guard logic: a failed send waits for you to approve it again, and an uncertain send is checked against the channel before any retry, so nothing is sent twice.</p>
   <div class="row">${['ok','fail','timeout_after_send','timeout_before_send'].map(m=>`<button class="btn" onclick="act(async()=>{await api('/demo/channel-mode','POST',{mode:'${m}'});toast('Channel mode: ${m}')})">${m}</button>`).join('')}</div></div>
   <div class="card"><h3>Honest status</h3><p class="sm mut" style="margin:0 0 8px">Handoffs show "escalated" after you advance the clock because the simulated time passed their response deadline (SLA) without anyone acknowledging them.</p><ul class="sm" style="padding-left:18px"><li>Everything here runs offline against simulators.</li><li>The model extractor and the GHL adapter are unit-tested with fakes and a mocked transport only.</li><li>No live model, CRM, email or WhatsApp call has ever been made by this build.</li></ul></div></div>`;
}
window.adv=h=>act(async()=>{await api('/demo/advance','POST',{hours:h});toast('Advanced '+h+'h')});
(function(){const [r,p]=location.hash.slice(1).split('/');if(r&&NAV.some(n=>n[0]===r)){ROUTE=r;PARAM=p||null}render()})();
const markScroll=()=>document.querySelectorAll('.tbl').forEach(e=>e.classList.toggle('scrolls',e.scrollWidth>e.clientWidth+2));
new MutationObserver(()=>requestAnimationFrame(markScroll)).observe(document.getElementById('view'),{childList:true,subtree:true});window.addEventListener('resize',markScroll);
setInterval(()=>{if(!document.hidden&&['overview','review','handoffs'].includes(ROUTE)&&!document.activeElement.matches('textarea,input'))render()},30000);
