/* SneakPeek — app.js */
'use strict';

let cfg = {}, alertCount = 0;

// ── Clock ──────────────────────────────────────────────────────
setInterval(()=>{
  const n = new Date();
  const el = document.getElementById('clk');
  if (el) el.textContent =
    n.toLocaleTimeString('en-GB',{hour12:false})+' '+n.toLocaleDateString('en-GB');
}, 1000);

// ── Toast ──────────────────────────────────────────────────────
function toast(msg, err=false){
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.className   = 'toast show' + (err ? ' err' : '');
  clearTimeout(t._t);
  t._t = setTimeout(()=> t.classList.remove('show'), 3500);
}

// ── Gauge (half-circle) ────────────────────────────────────────
function drawGauge(score){
  const c = document.getElementById('gc'); if (!c) return;
  const x = c.getContext('2d');
  const cx=80, cy=88, r=68, sa=Math.PI, arc=Math.PI;
  x.clearRect(0,0,c.width,c.height);
  x.beginPath(); x.arc(cx,cy,r,sa,sa+arc); x.lineWidth=10; x.strokeStyle='#1a1a1a'; x.stroke();
  [{max:.3,c:'#00ff88'},{max:.5,c:'#ffaa00'},{max:.7,c:'#ff8800'},{max:1,c:'#ff2244'}]
    .reduce((prev,s)=>{
      const f=Math.min(score,s.max);
      if(f>prev){
        x.beginPath(); x.arc(cx,cy,r,sa+(prev)*arc,sa+(f)*arc);
        x.lineWidth=10; x.strokeStyle=s.c; x.lineCap='butt'; x.stroke();
      } return s.max;
    },0);
  const na=sa+score*arc;
  x.beginPath(); x.moveTo(cx,cy); x.lineTo(cx+(r-6)*Math.cos(na),cy+(r-6)*Math.sin(na));
  x.lineWidth=1.5; x.strokeStyle='#fff'; x.lineCap='round'; x.stroke();
  x.beginPath(); x.arc(cx,cy,3,0,2*Math.PI); x.fillStyle='#fff'; x.fill();
}

// ── State → UI ─────────────────────────────────────────────────
function applyState(st){
  const s=st.sensor||{}, p=st.pipeline||{}, sys=st.system||{}, cam=st.camera||{};

  // Dots
  pd('d-motion', s.motion);
  pd('d-smoke',  s.smoke, 'danger');
  pd('d-night',  s.is_night, 'amber');
  const online = !!cam.online || (sys.running && (Date.now()/1000 - (sys.last_update||0)) < 8);
  pd('d-sys', online);
  const sl = document.getElementById('sys-lbl');
  if(sl){ sl.textContent=online?'ONLINE':'OFFLINE'; sl.className=online?'on':''; }

  // FPS
  const fp=document.getElementById('fps-el');
  if(fp) fp.textContent = p.fps ? (p.fps+' fps') : '— fps';

  // Camera badge
  const cb=document.getElementById('cam-badge');
  if(cb) cb.textContent = online ? '● LIVE' : '● OFFLINE';

  // Sensor bars
  const mot = s.motion, dur = s.motion_duration_s||0;
  bar('sb-mot', mot ? Math.min(100, dur/30*100) : 0);
  txt('sv-mot', mot ? dur.toFixed(0)+'s' : 'OFF');

  bar('sb-smk', Math.min(100,(s.smoke_ppm||0)/600*100));
  txt('sv-smk', (s.smoke_ppm||0).toFixed(0));

  bar('sb-ldr', ((s.ldr||0)/4095)*100);
  txt('sv-ldr', s.ldr||0);

  bar('sb-dur', Math.min(100, dur/30*100));
  txt('sv-dur', dur.toFixed(0)+'s');

  // Context pills
  cp('cp-day',   !s.is_night, false);
  cp('cp-night',  s.is_night, false);
  cp('cp-smk',    s.smoke,    true);
  cp('cp-mot',    s.motion,   false);

  // Score gauge
  const score = p.score||0;
  drawGauge(score);
  const lv = score<.3?'CLEAR':score<.5?'WARNING':score<.7?'ALERT':'CRITICAL';
  const lc = score<.3?'#00ff88':score<.5?'#ffaa00':score<.7?'#ff8800':'#ff2244';
  const sn = document.getElementById('snum');
  if(sn){ sn.textContent=score.toFixed(2); sn.style.color=lc; }
  const sl2=document.getElementById('slv');
  if(sl2){ sl2.textContent=lv; }
  const sb=document.getElementById('sbar');
  if(sb){ sb.style.width=Math.min(100,score*100)+'%'; sb.style.background=lc; }

  // Pipeline stages
  const stMap = {
    idle:[], detecting:['ps-gate','ps-yolo'],
    recognizing:['ps-gate','ps-yolo','ps-face'],
    posing:['ps-gate','ps-yolo','ps-face','ps-pose'],
    scoring:['ps-gate','ps-yolo','ps-face','ps-pose','ps-score'],
    alert:['ps-gate','ps-yolo','ps-face','ps-pose','ps-score']
  };
  const allPs=['ps-gate','ps-yolo','ps-face','ps-pose','ps-score'];
  const active=stMap[p.stage]||[];
  allPs.forEach(id=>{
    const el=document.getElementById(id); if(!el) return;
    el.classList.remove('active','done','alert');
    if(p.stage==='alert'&&active.includes(id)) el.classList.add('alert');
    else if(active.includes(id))
      el.classList.add(id===active[active.length-1]?'active':'done');
  });

  txt('pm-ppl', p.person_count!=null ? p.person_count : '—');
  txt('pm-unk', p.unknown_count!=null ? p.unknown_count : '—');
  txt('pm-stg', (p.stage||'idle').toUpperCase());
  txt('pm-thr', (p.last_threats||[]).length
    ? p.last_threats.slice(0,2).join(', ').toUpperCase() : '—');
}

// Helpers
function pd(id,on,mode=''){
  const el=document.getElementById(id); if(!el) return;
  el.className='pd'+(on?(mode?' '+mode:' on'):'');
}
function bar(id,pct){const el=document.getElementById(id);if(el)el.style.width=Math.min(100,Math.max(0,pct))+'%';}
function txt(id,v){const el=document.getElementById(id);if(el)el.textContent=v;}
function cp(id,on,danger){
  const el=document.getElementById(id); if(!el) return;
  el.className='cp'+(on?(danger?' danger':' on'):'');
}

// ── SSE ────────────────────────────────────────────────────────
let _sseT;
function connectSSE(){
  const es = new EventSource('/api/sse');
  es.addEventListener('init', e => applyState(JSON.parse(e.data)));
  es.addEventListener('state_update', e => applyState(JSON.parse(e.data)));
  es.addEventListener('sensor_update', async ()=>{
    const st=await(await fetch('/api/state')).json();
    applyState(st);
  });
  es.addEventListener('threat_update', async ()=>{
    const st=await(await fetch('/api/state')).json();
    applyState(st);
  });
  es.addEventListener('camera_status', async ()=>{
    const st=await(await fetch('/api/state')).json();
    applyState(st);
  });
  es.addEventListener('alert_sent', async ()=>{
    const data=await(await fetch('/api/alerts')).json();
    alertCount = data.length; updateBadge(); updateAlertTotal();
    if(document.getElementById('tab-alerts').classList.contains('active')) loadAlerts();
  });
  es.onerror=()=>{ es.close(); clearTimeout(_sseT); _sseT=setTimeout(connectSSE,3000); };
}
function updateBadge(){
  const b=document.getElementById('abadge');
  if(b){ b.style.display=alertCount?'inline':'none'; b.textContent=alertCount; }
}
function updateAlertTotal(){
  const el=document.getElementById('al-total');
  if(el) el.textContent=alertCount+' alert'+(alertCount===1?'':'s');
}

// ── Tabs ───────────────────────────────────────────────────────
document.querySelectorAll('.tab').forEach(btn=>{
  btn.addEventListener('click',()=>{
    document.querySelectorAll('.tab').forEach(t=>t.classList.remove('active'));
    document.querySelectorAll('.page').forEach(p=>p.classList.remove('active'));
    btn.classList.add('active');
    const tabId = 'tab-'+btn.dataset.tab;
    document.getElementById(tabId)?.classList.add('active');
    ({alerts:loadAlerts, enroll:loadFaces, threats:loadThreats, config:loadConfig})[btn.dataset.tab]?.();
  });
});

// ── ALERTS — timeline style ─────────────────────────────────────
async function loadAlerts(){
  const data=await(await fetch('/api/alerts')).json();
  alertCount = data.length; updateBadge(); updateAlertTotal();
  const tl=document.getElementById('timeline');
  if(!data.length){ tl.innerHTML='<div class="tl-empty">No alerts recorded yet</div>'; return; }
  // Group by date
  const groups = {};
  data.forEach(a=>{
    const dateKey = a.timestamp ? a.timestamp.substring(0,10) : 'Unknown';
    if(!groups[dateKey]) groups[dateKey] = [];
    groups[dateKey].push(a);
  });
  tl.innerHTML = Object.entries(groups).map(([date, alerts])=>`
    <div class="tl-date">${formatDate(date)}</div>
    ${alerts.map(tlItemHTML).join('')}
  `).join('');
}

function formatDate(dateStr){
  try{
    const d=new Date(dateStr);
    return d.toLocaleDateString('en-GB',{weekday:'long',day:'numeric',month:'long',year:'numeric'});
  }catch(e){ return dateStr; }
}

function tlItemHTML(a){
  const timeStr = a.timestamp ? a.timestamp.substring(11,19) : '—';
  const score   = a.score||0;
  const sevClass = score<.3?'score-clear':score<.5?'score-warn':score<.7?'score-alert':'score-crit';
  const snap    = a.snapshot
    ? `<img src="/snapshots/${a.snapshot}" alt="">`
    : `<span class="ns">NO SNAP</span>`;
  const tags    = (a.threats||[]).map(t=>{
    const cls = t==='smoke_or_gas'?'smoke':t.includes('known')?'known':'';
    return `<span class="tl-tag ${cls}">${t.replace(/_/g,' ').toUpperCase()}</span>`;
  }).join('');
  const sensor  = a.sensor||{};
  const ctxBits = [
    sensor.is_night ? `<span class="tl-cx lit">NIGHT</span>` : `<span class="tl-cx grn">DAY</span>`,
    sensor.smoke    ? `<span class="tl-cx lit">SMOKE ${(sensor.smoke_ppm||0).toFixed(0)}ppm</span>` : '',
    sensor.motion_duration_s>0 ? `<span class="tl-cx">MOT ${sensor.motion_duration_s.toFixed(0)}s</span>` : '',
  ].filter(Boolean).join('');
  return `
  <div class="tl-item">
    <div class="tl-time">${timeStr}</div>
    <div class="tl-sev ${sevClass}"></div>
    <div class="tl-snap">${snap}</div>
    <div class="tl-body">
      <div class="tl-score">Score <strong>${score.toFixed(2)}</strong></div>
      <div class="tl-threats">${tags}</div>
      <div class="tl-reason">${(a.reasons||[]).join(' · ')}</div>
      <div class="tl-ctx">${ctxBits}</div>
    </div>
  </div>`;
}

function prependTimelineAlert(a){
  const tl=document.getElementById('timeline');
  if(!tl) return;
  const empty=tl.querySelector('.tl-empty');
  if(empty) tl.innerHTML='';
  // Add today header if needed
  const today=new Date().toISOString().substring(0,10);
  if(!tl.querySelector(`[data-date="${today}"]`)){
    const hdr=document.createElement('div');
    hdr.className='tl-date'; hdr.dataset.date=today;
    hdr.textContent=formatDate(today);
    tl.prepend(hdr);
  }
  const div=document.createElement('div');
  div.innerHTML=tlItemHTML(a);
  const hdr=tl.querySelector(`[data-date="${today}"]`);
  if(hdr) hdr.insertAdjacentHTML('afterend',tlItemHTML(a));
  else tl.insertAdjacentHTML('afterbegin',tlItemHTML(a));
}

async function clearAlerts(){
  if(!confirm('Clear all alerts?')) return;
  await fetch('/api/alerts/clear',{method:'POST'});
  alertCount=0; updateBadge(); updateAlertTotal();
  loadAlerts(); toast('Alert log cleared');
}

// ── Enrollment ─────────────────────────────────────────────────
async function loadFaces(){
  const data=await(await fetch('/api/faces')).json();
  const names = Array.isArray(data) ? data : (data.identities||[]).map(x=>x.name);
  const list=document.getElementById('id-list');
  if(!names.length){list.innerHTML='<div class="empty">No identities added</div>';return;}
  list.innerHTML=names.map(name=>`
    <div class="id-row">
      <div><div class="id-nm">${name}</div><div class="id-ct">enrolled</div></div>
      <div style="display:flex;align-items:center;gap:8px"><span class="id-enr">✓ ENROLLED</span></div>
    </div>`).join('');
}
const dz=document.getElementById('dz'), fi=document.getElementById('ffiles');
if(dz){ dz.addEventListener('click',()=>fi.click());
  dz.addEventListener('dragover',e=>{e.preventDefault();dz.classList.add('over')});
  dz.addEventListener('dragleave',()=>dz.classList.remove('over'));
  dz.addEventListener('drop',e=>{e.preventDefault();dz.classList.remove('over');fi.files=e.dataTransfer.files;showChips(fi.files)});
}
if(fi) fi.addEventListener('change',()=>showChips(fi.files));
function showChips(files){
  document.getElementById('fchips').innerHTML=
    Array.from(files).map(f=>`<span class="fchip">${f.name}</span>`).join('');
}
async function uploadFace(){
  const name=document.getElementById('en-name')?.value.trim();
  const files=fi?.files;
  const msg=document.getElementById('up-msg');
  if(!name){showMsg(msg,'Enter a name','err');return;}
  if(!files?.length){showMsg(msg,'Select photos','err');return;}
  const fd=new FormData(); fd.append('name',name);
  for(const f of files) fd.append('photos',f);
  const data=await(await fetch('/api/faces/upload',{method:'POST',body:fd})).json();
  if(data.ok){showMsg(msg,`Uploaded ${data.saved.length} photo(s)`,'ok');
    document.getElementById('en-name').value='';
    document.getElementById('fchips').innerHTML='';loadFaces();}
  else showMsg(msg,data.error||'Failed','err');
}
async function delFace(n){
  if(!confirm(`Delete ${n}?`))return;
  await fetch(`/api/faces/${encodeURIComponent(n)}`,{method:'DELETE'});
  loadFaces(); toast(`${n} removed`);
}
async function runEnroll(){
  const btn=document.getElementById('enrl-btn');
  const out=document.getElementById('enrl-out');
  btn.disabled=true; btn.textContent='RUNNING...';
  out.style.display='block'; out.textContent='Running...\n';
  const data=await(await fetch('/api/faces/enroll',{method:'POST'})).json();
  btn.disabled=false; btn.textContent='RUN ENROLLMENT';
  out.textContent=data.output||(data.error||'Done');
  toast(data.ok?'Enrollment complete':'Failed',!data.ok);
}

// ── Threats ─────────────────────────────────────────────────────
const SEVL={low:'LOW',medium:'MEDIUM',high:'HIGH',critical:'CRITICAL'};
async function loadThreats(){
  const data=await(await fetch('/api/custom_threats')).json();
  renderThreats(data);
  cfg=await(await fetch('/api/config')).json();
  renderWeights();
}
function renderThreats(threats){
  const list=document.getElementById('ct-list');
  const cnt=document.getElementById('ct-cnt');
  if(cnt) cnt.textContent=threats.length+' threat'+(threats.length===1?'':'s');
  if(!threats.length){list.innerHTML='<div class="empty">No threats defined yet</div>';return;}
  list.innerHTML=threats.map(t=>{
    const rc='ct-row '+(t.enabled?'on':'off')+(t.severity==='high'?' sev-h':t.severity==='critical'?' sev-c':'');
    const date=t.created_at?new Date(t.created_at).toLocaleDateString():'';
    return `<div class="${rc}" id="cr-${t.id}">
      <div class="ct-tog"><label class="tog">
        <input type="checkbox" ${t.enabled?'checked':''} onchange="toggleT('${t.id}',this.checked)">
        <span class="tog-sl"></span></label></div>
      <div class="ct-body">
        <div class="ct-desc" id="cd-${t.id}">${esc(t.description)}</div>
        <textarea class="ct-edit-fi" id="ce-${t.id}">${esc(t.description)}</textarea>
        <div class="ct-meta">
          <span class="stag s-${t.severity||'medium'}">${SEVL[t.severity]||t.severity}</span>
          ${date?`<span class="ct-date">${date}</span>`:''}
        </div>
      </div>
      <div class="ct-acts">
        <button class="ct-btn" onclick="editT('${t.id}')">EDIT</button>
        <button class="ct-btn del" onclick="delT('${t.id}')">DEL</button>
      </div></div>`;
  }).join('');
}
function renderWeights(){
  const wl=document.getElementById('wt-list'); if(!wl) return;
  wl.innerHTML=Object.entries(cfg.weights||{}).map(([k,v])=>`
    <div class="wt-row">
      <div class="wt-lbl">${k.replace(/_/g,' ').toUpperCase()}<span id="wv-${k}">${parseFloat(v).toFixed(2)}</span></div>
      <input type="range" class="slider" id="ws-${k}" min="0" max="1" step="0.01" value="${v}"
        oninput="document.getElementById('wv-${k}').textContent=parseFloat(this.value).toFixed(2)">
    </div>`).join('');
  const nm=document.getElementById('nmul');
  if(nm){ nm.value=cfg.night_multiplier||1.8;
    const nv=document.getElementById('nmul-v');
    if(nv) nv.textContent=parseFloat(nm.value).toFixed(1)+'x'; }
}
async function addThreat(){
  const desc=document.getElementById('ct-desc')?.value.trim();
  const sev=document.getElementById('ct-sev')?.value;
  const msg=document.getElementById('ct-msg');
  if(!desc){showMsg(msg,'Write a description','err');return;}
  const data=await(await fetch('/api/custom_threats',{
    method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({description:desc,severity:sev})
  })).json();
  if(data.ok){
    showMsg(msg,'Threat added','ok');
    document.getElementById('ct-desc').value='';
    loadThreats(); setTimeout(()=>showMsg(msg,'',''),2500);
  } else showMsg(msg,data.error||'Failed','err');
}
async function toggleT(id,en){
  await fetch(`/api/custom_threats/${id}`,{
    method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({enabled:en})
  });
  const r=document.getElementById(`cr-${id}`);
  if(r){r.classList.toggle('on',en);r.classList.toggle('off',!en);}
}
function editT(id){
  const d=document.getElementById(`cd-${id}`);
  const e=document.getElementById(`ce-${id}`);
  const b=document.querySelector(`#cr-${id} .ct-btn:not(.del)`);
  if(!e.style.display||e.style.display==='none'){
    d.style.display='none';e.style.display='block';e.focus();b.textContent='SAVE';
  } else saveT(id);
}
async function saveT(id){
  const e=document.getElementById(`ce-${id}`);
  const d=document.getElementById(`cd-${id}`);
  const b=document.querySelector(`#cr-${id} .ct-btn:not(.del)`);
  const v=e.value.trim(); if(!v) return;
  await fetch(`/api/custom_threats/${id}`,{
    method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({description:v})
  });
  d.textContent=v;d.style.display='block';e.style.display='none';b.textContent='EDIT';
  toast('Updated');
}
async function delT(id){
  if(!confirm('Delete?'))return;
  await fetch(`/api/custom_threats/${id}`,{method:'DELETE'});
  loadThreats(); toast('Deleted');
}
async function saveWeights(){
  if(!cfg.weights) return;
  Object.keys(cfg.weights).forEach(k=>{
    const el=document.getElementById(`ws-${k}`);
    if(el) cfg.weights[k]=parseFloat(el.value);
  });
  const nm=document.getElementById('nmul');
  if(nm) cfg.night_multiplier=parseFloat(nm.value);
  const data=await(await fetch('/api/config',{
    method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(cfg)
  })).json();
  const msg=document.getElementById('wt-msg');
  const ok = !!(data.ok || data.status === 'saved');
  showMsg(msg, ok?'Weights saved — active in engine within 1s':'Error', ok?'ok':'err');
  setTimeout(()=>showMsg(msg,'',''),3000);
}

// ── Config ─────────────────────────────────────────────────────
async function loadConfig(){
  cfg=await(await fetch('/api/config')).json();
  // Thresholds
  const tf=document.getElementById('th-fields');
  if(tf) tf.innerHTML=Object.entries(cfg.thresholds||{}).map(([k,v])=>`
    <div class="cfg-field">
      <div class="cfg-lbl">${k.replace(/_/g,' ').toUpperCase()}<span id="tv-${k}">${v}</span></div>
      <input type="number" class="fi" id="tf-${k}" value="${v}" step="0.01"
        oninput="document.getElementById('tv-${k}').textContent=this.value">
    </div>`).join('');
  // Cooldowns
  const cf=document.getElementById('cd-fields');
  if(cf) cf.innerHTML=Object.entries(cfg.cooldown_seconds||{}).map(([k,v])=>`
    <div class="cfg-field">
      <div class="cfg-lbl">${k.replace(/_/g,' ').toUpperCase()}<span id="cv-${k}">${v}s</span></div>
      <input type="number" class="fi" id="cf-${k}" value="${v}" min="0"
        oninput="document.getElementById('cv-${k}').textContent=this.value+'s'">
    </div>`).join('');
  const cam=cfg.camera||{};
  setVal('cfg-url',   cam.phone_url||'');
  setVal('cfg-skip',  cam.process_every_n_frames||5);
  setVal('cfg-port',  cfg.serial?.port||'');
  setVal('cfg-email-provider', cfg.email_provider||'smtp');
  setVal('cfg-email', cfg.aws?.alert_email||'');
  setVal('cfg-smtp-from', cfg.smtp?.from || getVal('cfg-email'));
  setVal('cfg-smtp-host', cfg.smtp?.host || 'smtp.gmail.com');
  setVal('cfg-smtp-port', cfg.smtp?.port || 587);
  setVal('cfg-smtp-user', cfg.smtp?.user || getVal('cfg-email'));
  setVal('cfg-smtp-pass', cfg.smtp?.pass || '');
  setVal('cfg-smtp-tls', String(cfg.smtp?.use_tls ?? true));
  setVal('cfg-apigw', cfg.aws?.api_gateway_url||'');
  setVal('cfg-bucket',cfg.aws?.s3_bucket||'');
  if(cfg.claude_api_key){
    const el=document.getElementById('cfg-claude');
    if(el) el.placeholder='sk-ant-... (saved)';
  }
}
async function saveConfig(){
  const msg=document.getElementById('cfg-msg');
  if(!cfg.thresholds) cfg=await(await fetch('/api/config')).json();
  Object.keys(cfg.thresholds||{}).forEach(k=>{
    const el=document.getElementById(`tf-${k}`);if(el)cfg.thresholds[k]=parseFloat(el.value);});
  Object.keys(cfg.cooldown_seconds||{}).forEach(k=>{
    const el=document.getElementById(`cf-${k}`);if(el)cfg.cooldown_seconds[k]=parseInt(el.value);});
  cfg.camera=cfg.camera||{};
  cfg.camera.phone_url=getVal('cfg-url');
  cfg.camera.process_every_n_frames=parseInt(getVal('cfg-skip'))||5;
  cfg.camera.source='phone';
  cfg.serial=cfg.serial||{};
  cfg.serial.port=getVal('cfg-port');
  cfg.email_provider=getVal('cfg-email-provider') || 'smtp';
  cfg.aws=cfg.aws||{};
  cfg.aws.alert_email=getVal('cfg-email');
  cfg.aws.api_gateway_url=getVal('cfg-apigw');
  cfg.aws.s3_bucket=getVal('cfg-bucket');
  cfg.smtp=cfg.smtp||{};
  cfg.smtp.from=getVal('cfg-smtp-from');
  cfg.smtp.host=getVal('cfg-smtp-host');
  cfg.smtp.port=parseInt(getVal('cfg-smtp-port')) || 587;
  cfg.smtp.user=getVal('cfg-smtp-user');
  cfg.smtp.pass=getVal('cfg-smtp-pass');
  cfg.smtp.use_tls=getVal('cfg-smtp-tls') === 'true';
  const ck=getVal('cfg-claude');
  if(ck) cfg.claude_api_key=ck;
  const data=await(await fetch('/api/config',{
    method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(cfg)
  })).json();
  const ok = !!(data.ok || data.status === 'saved');
  showMsg(msg, ok?'Saved — engine reloads within 1s':'Error saving', ok?'ok':'err');
  setTimeout(()=>showMsg(msg,'',''),4000);
  toast(ok?'Config saved':'Save failed',!ok);
}

// ── Helpers ─────────────────────────────────────────────────────
function esc(s){return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}
function setVal(id,v){const el=document.getElementById(id);if(el)el.value=v;}
function getVal(id){const el=document.getElementById(id);return el?el.value:'';}
function showMsg(el,msg,cls){if(el){el.textContent=msg;el.className='msg '+(cls||'');}}

// ── Init ───────────────────────────────────────────────────────
(async()=>{
  drawGauge(0);
  const st=await(await fetch('/api/state')).json(); applyState(st);
  const alerts=await(await fetch('/api/alerts')).json();
  alertCount=alerts.length; updateBadge(); updateAlertTotal();
  connectSSE();
  setInterval(async ()=>{
    try{
      const fresh=await(await fetch('/api/state')).json();
      applyState(fresh);
    }catch(_e){}
  }, 1200);
})();
