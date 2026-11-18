"use strict";
const $ = id => document.getElementById(id);
const text = (id, value) => { $(id).textContent = value; };
const esc = value => String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"})[c]);
const fmt = value => Number.isFinite(value) ? value.toFixed(4) : "—";
const url = (path, args) => path + "?" + new URLSearchParams(args).toString();
const labels = {queued:"排队中", running:"训练中", stopping:"停止中", completed:"已完成", failed:"失败",
  stopped:"已停止", interrupted:"已中断", recorded:"历史记录", smoke:"冒烟检查",
  stopped_at_epoch_boundary:"轮次边界暂停", stopped_by_cap:"提前停止"};
const plots = {training_curves:"训练曲线",per_class_auc:"逐类 AUC 图",per_class:"逐类指标图",
  roc_pr:"ROC / PR",roc:"ROC",pr:"PR",confusion_matrices:"混淆矩阵",class_distribution:"类别分布"};

let credential = new URLSearchParams(location.hash.slice(1)).get("token") || sessionStorage.getItem("trainboard-token") || "";
if (credential) sessionStorage.setItem("trainboard-token", credential);
history.replaceState(null, "", location.pathname);
let catalog, state = {jobs:[]}, runs = [], selected = "", jobId = "", tab = "score";
let following = false, detail = null, lastRuns = 0, busy = false, timer;
let lastQueue = "", lastLog = "", lastPlot = "";

async function get(path, options) {
  const r = await fetch(path, {...options, headers: {"X-Panel-Token":credential, ...options?.headers}});
  const body = await r.json();
  if (!r.ok) throw new Error(body.error || "请求失败");
  return body;
}
function notify(message, error=false) {
  const box = $("notice");
  box.textContent = message;
  box.className = error ? "notice error" : "notice";
  box.hidden = false;
  clearTimeout(timer);
  timer = setTimeout(() => { box.hidden = true; }, 3600);
}
async function action(name, id, task, message) {
  try {
    await get("/api/control",{method:"POST",headers:{"Content-Type":"application/json","X-Panel-Token":credential},
      body:JSON.stringify({action:name,id,task})});
    if (message) notify(message);
    await refresh();
  } catch (e) { notify(e.message,true); }
}
function optionsHTML(list, selectedValue, label) {
  return list.map(item => '<option value="' + esc(item.id) + '"' + (item.id===selectedValue ? " selected" : "") + ">" +
    esc(label(item)) + "</option>").join("");
}
function hydrate() {
  const groups = [...new Set(Object.values(catalog.entries).map(e => e.group))];
  $("entry").innerHTML = groups.map(group => '<optgroup label="'+esc(group)+'">' +
    optionsHTML(Object.entries(catalog.entries).filter(([,e]) => e.group===group).map(([id,e])=>({id,...e})),
      Object.keys(catalog.entries)[0],e=>e.label) + "</optgroup>").join("");
  document.title=catalog.title;
  document.querySelector(".brand strong").textContent=catalog.title;
  if(catalog.readonly){document.querySelector("#composer button[type=submit]").disabled=true;$("source").disabled=true;}
  updateFields();
}
function updateFields() {
  const entry = catalog.entries[$("entry").value];
  const specs = entry?.parameters || {};
  $("parameters").innerHTML = Object.entries(specs).map(([key, spec]) => {
    const disabled = catalog.readonly ? " disabled" : "";
    const attrs = ' name="'+esc(key)+'"' + disabled;
    const input = spec.choices ? '<select'+attrs+'><option value="">默认</option>'+
      spec.choices.map(v=>'<option value="'+esc(v)+'">'+esc(v)+'</option>').join("")+'</select>' :
      '<input'+attrs+' type="'+((spec.type||"str")==="str"?"text":"number")+'"'+
      (spec.min!==undefined?' min="'+esc(spec.min)+'"':"")+
      (spec.max!==undefined?' max="'+esc(spec.max)+'"':"")+
      (spec.type==="float"?' step="any"':"")+' placeholder="'+esc(spec.default??"默认")+'">';
    return '<label>'+esc(spec.label||key)+input+'</label>';
  }).join("");
  text("option-note",catalog.readonly?"只读观测模式":"留空沿用入口默认配置。");
}

function badge(state) {
  return '<span class="status '+(state==="completed"?"good":state==="running"?"live":state==="failed"?"bad":"")+
    '">'+esc(labels[state]||state)+"</span>";
}
function progress(job) {
  const p=job.progress || {};
  if (job.state==="completed") return 1;
  if (!p.epoch || !p.epochs) return 0;
  const fraction = p.phase==="epoch_saved" ? 1 : p.phase==="valid" ? .96 : p.batches ? .93 * p.batch/p.batches : 0;
  return Math.max(0,Math.min(1,(p.epoch-1+fraction)/p.epochs));
}
function renderQueue() {
  const active = state.jobs.find(j=>j.id===state.current);
  const waiting = state.jobs.filter(j=>j.state==="queued");
  const finished = state.jobs.filter(j=>j.id!==state.current && j.state!=="queued").reverse();
  const jobs = [...(active?[active]:[]),...waiting,...finished];
  text("count-queue",waiting.length);
  text("active-model",active?.label || "空闲");
  text("active-phase",active?.progress.epoch ? "第 " + active.progress.epoch + "/" + (active.progress.epochs||"—") + " 轮" : active?"准备环境":"等待开始");
  text("queue-message",state.message);
  text("queue-state",state.enabled?"正在派发":"已暂停");
  $("queue-state").className="status " + (state.enabled?"good":"");
  $("start").disabled=catalog.readonly || !!state.enabled || (!waiting.length&&!active);
  $("pause").disabled=catalog.readonly || !state.enabled;
  if (active && !jobId) jobId=active.id;
  $("log-task").innerHTML = '<option value="">选择任务日志</option>' + optionsHTML(jobs,jobId,j=>j.label+" · "+(labels[j.state]||j.state));
  const html = jobs.length ? jobs.map(job=>{
    const p=job.progress;
    const args=[job.group,...Object.entries(job.options).map(([k,v])=>k+" "+v)].join(" · ");
    const detailText = p.detail || (p.epoch ? "第 "+p.epoch+"/"+(p.epochs||"—")+" 轮" : job.message || "");
    const controls = catalog.readonly ? '<button data-q="show" class="secondary">看日志</button>' : job.state==="queued" ?
      '<button data-q="up" aria-label="上移" class="secondary">↑</button><button data-q="down" aria-label="下移" class="secondary">↓</button><button data-q="remove" class="secondary">移除</button>' :
      '<button data-q="show" class="secondary">看日志</button>'+(job.state==="running"?'<button data-q="stop" class="secondary">停止</button>':"");
    return '<article class="task '+(job.id===jobId?"focus":"")+'" data-job="'+esc(job.id)+'"><div class="task-top"><strong>'+esc(job.label)+
      "</strong>"+badge(job.state)+'</div><p class="small muted">'+esc(args)+"</p>"+
      '<progress max="1" value="'+progress(job)+'" aria-label="训练进度"></progress>'+
      '<div class="task-detail">'+esc(detailText)+(Number.isFinite(p.auc)?" · 指标 "+fmt(p.auc):"")+
      '</div><div class="task-actions">'+controls+"</div></article>";
  }).join("") : '<div class="empty">队列空闲。可添加单项任务，或载入左侧实验预设。</div>';
  if (html!==lastQueue) { $("queue-list").innerHTML=html; lastQueue=html; }
}
function runOptions() {
  const search=$("run-search").value.toLowerCase().trim();
  const filtered=runs.filter(r=>[r.id,r.name,String(r.seed??"")].join(" ").toLowerCase().includes(search));
  const visible=filtered.some(r=>r.id===selected);
  $("run").innerHTML=(visible?"":'<option value="">'+(filtered.length?"选择训练运行":"无匹配运行")+"</option>")+
    optionsHTML(filtered,selected,r=>r.name+" · s"+(r.seed??"—")+" · "+fmt(r.auc)+" · "+r.id.split("/").pop());
  text("count-runs",runs.length);
  const best=runs.filter(r=>r.status==="completed"&&Number.isFinite(r.auc)).sort((a,b)=>catalog.metric_mode==="min"?a.auc-b.auc:b.auc-a.auc)[0];
  text("best-auc",fmt(best?.auc));
  text("best-model",best?.name || catalog.primary_metric);
}
const colors=["#08796d","#3468c1"];
const sv = (title,body) => '<svg viewBox="0 0 700 260" role="img" aria-label="'+esc(title)+'">'+body+"</svg>";
const svgText = (x,y,t,anchor="middle",color="#476477") =>
  '<text x="'+x+'" y="'+y+'" text-anchor="'+anchor+'" fill="'+color+'" font-size="12">'+esc(t)+"</text>";
function lineChart(rows, defs, title) {
  const valid=rows.flatMap(r=>defs.map(d=>r[d[0]])).filter(Number.isFinite);
  if (!valid.length) return "";
  const min=Math.min(...valid), max=Math.max(...valid);
  const pad=Math.max((max-min)*.09,.005), lo=min-pad, hi=max+pad;
  const left=60, right=672, top=26, bottom=211;
  const first=rows[0].epoch, last=rows.at(-1).epoch;
  const x=e=>first===last?(left+right)/2:left+(e-first)/(last-first)*(right-left);
  const y=v=>bottom-(v-lo)/(hi-lo)*(bottom-top);
  let body="";
  for(let i=0;i<=4;i++){
    const v=lo+(hi-lo)*i/4, py=y(v);
    body+='<line x1="'+left+'" x2="'+right+'" y1="'+py+'" y2="'+py+'" stroke="#e2ebf0"/>'+svgText(left-8,py+4,v.toFixed(3),"end");
  }
  const ticks=rows.filter((_,i)=>i===0||i===rows.length-1||i%Math.ceil(rows.length/7)===0);
  for(const row of ticks) body+=svgText(x(row.epoch),232,row.epoch);
  body+=svgText(right,251,"训练轮次","end");
  defs.forEach(([key,name],i)=>{
    let points=[];
    for(const r of rows){
      if (!Number.isFinite(r[key])) { points=[]; continue; }
      const px=x(r.epoch),py=y(r[key]),color=colors[i];
      if(points.length) body+='<line x1="'+points[0]+'" y1="'+points[1]+'" x2="'+px+'" y2="'+py+'" stroke="'+color+'" stroke-width="2.4"/>';
      body+='<circle cx="'+px+'" cy="'+py+'" r="3.3" fill="'+color+'"><title>第 '+r.epoch+' 轮 · '+esc(name)+' '+fmt(r[key])+"</title></circle>";
      points=[px,py];
    }
    body+='<line x1="'+(80+i*135)+'" x2="'+(97+i*135)+'" y1="13" y2="13" stroke="'+colors[i]+'" stroke-width="3"/>'+
      svgText(103+i*135,17,name,"start");
  });
  return sv(title,body);
}
function barChart(rows) {
  if (!rows.some(r=>Number.isFinite(r.auc))) return "";
  let body=svgText(637,20,"最佳检查点逐类 AUC","end");
  rows.forEach((r,i)=>{
    if(!Number.isFinite(r.auc)) return;
    const y=30+21*i;
    body+=svgText(98,y+11,r.name,"end")+
      '<rect x="111" y="'+y+'" width="492" height="13" rx="3" fill="#e7eff4"/>'+
      '<rect x="111" y="'+y+'" width="'+Math.max(0,Math.min(1,r.auc))*492+'" height="13" rx="3" fill="#1a8e80">'+
      "<title>"+esc(r.name)+" "+fmt(r.auc)+"</title></rect>"+svgText(641,y+11,fmt(r.auc),"end");
  });
  return sv("逐类 AUC",body).replace("0 0 700 260", "0 0 700 "+Math.max(260,rows.length*21+50));
}
function renderDetail() {
  const keys=[...new Set((detail?.metrics||[]).flatMap(r=>Object.keys(r)))].filter(k=>k!=="epoch");
  const prev=$("metric").value || catalog.primary_metric;
  $("metric").innerHTML=keys.map(k=>'<option value="'+esc(k)+'"'+(k===prev?' selected':"")+'>'+esc(k)+'</option>').join("");
  const metric=$("metric").value;
  const chart = !detail?"":tab==="class"?barChart(detail.classes):
    tab==="loss"?lineChart(detail.metrics,[["train_loss","训练损失"],["valid_loss","验证损失"]],"训练与验证损失"):
    lineChart(detail.metrics,[[metric,metric]],metric);
  const content=chart||'<div class="chart-empty">暂无该视图的指标。训练完成一轮后，曲线会自动刷新。</div>';
  if(content!==lastPlot){$("chart").innerHTML=content;lastPlot=content;}
  $("open-run").disabled=!detail;
  if (!detail) { text("run-summary","选择一次历史运行，或跟踪当前训练。"); $("figures").innerHTML=""; return; }
  $("run-summary").innerHTML='<strong>'+esc(detail.name)+'</strong> · '+esc(detail.input??"—")+' px · Seed '+esc(detail.seed??"—")+
    ' · 第 '+esc(detail.epoch??0)+'/'+esc(detail.epochs??"—")+' 轮 · 最佳指标 '+fmt(detail.auc)+
    ' · 最佳轮 '+esc(detail.best_epoch??"—")+" · "+esc(detail.status==="running"?"日志记录为运行中":labels[detail.status]||detail.status);
  $("figures").innerHTML=detail.figures.map(name=>'<a target="_blank" rel="noopener" href="'+esc(url("/api/figure",{id:detail.id,name}))+
    '">'+esc(plots[name.replace(".png","")]||name)+"</a>").join("");
}
async function preview(path) {
  try {
    const content=await get(path);
    text("preview-title",content.path);
    text("preview-text",content.text);
    if (!$("preview").open) $("preview").showModal();
  }catch(e){notify(e.message,true);}
}
async function refresh() {
  if (busy || !catalog) return;
  busy=true;
  try {
    const reload=Date.now()-lastRuns>4000;
    const [fresh, known] = await Promise.all([get("/api/state"),reload?get("/api/runs"):runs]);
    state=fresh; runs=known;
    if (reload) lastRuns=Date.now();
    text("connection",catalog.readonly?"只读连接":"管理员连接"); $("connection").className="status dark";
    const running=state.jobs.find(j=>j.id===state.current);
    if(following && running){
      jobId=running.id;
      if(running.run) selected=running.run;
    }
    if(following && !running && state.jobs.length){
      const last=state.jobs.filter(j=>j.state!=="queued").at(-1);
      if(last){jobId=last.id; selected=last.run;}
    }
    if(!selected && !following && runs.length) selected=runs[0].id;
    renderQueue(); runOptions();
    const currentSelection=selected;
    const currentLog=jobId;
    const [report, log] = await Promise.all([
      currentSelection?get(url("/api/run",{id:currentSelection})).catch(()=>null):null,
      currentLog?get(url("/api/log",{id:currentLog})).catch(()=>({text:""})):{text:""},
    ]);
    if(currentSelection===selected){detail=report;renderDetail();}
    const logText=log.text || (currentLog?"等待该任务写入日志。":"面板启动的任务日志会出现在这里。");
    if(currentLog===jobId&&logText!==lastLog){
      const el=$("log"), stick=el.scrollHeight-el.scrollTop-el.clientHeight<55;
      el.textContent=logText;
      if(stick) el.scrollTop=el.scrollHeight;
      lastLog=logText;
    }
  } catch(e) { text("connection","连接中断");$("connection").className="status bad"; }
  finally {busy=false;}
}
async function boot() {
  try {catalog=await get("/api/catalog");hydrate();await refresh();}
  catch(e){notify(e.message,true);return;}
  setInterval(refresh,2200);
  $("entry").addEventListener("change",updateFields);
  $("composer").addEventListener("submit",e=>{
    e.preventDefault();
    const task={entry:$("entry").value};
    for(const [k,v] of new FormData(e.currentTarget)) if(v!=="") task[k]=((catalog.entries[task.entry].parameters[k].type||"str")==="str"?v:Number(v));
    action("add",null,task,"已加入队列");
  });
  $("metric").onchange=()=>renderDetail();
  $("figures").onclick=async e=>{
    const a=e.target.closest("a"); if(!a)return; e.preventDefault();
    try {
      const response=await fetch(a.href,{headers:{"X-Panel-Token":credential}});
      if(!response.ok)throw new Error("图片读取失败");
      const u=URL.createObjectURL(await response.blob());
      const link=document.createElement("a");link.href=u;link.download=a.textContent+".png";link.click();
      setTimeout(()=>URL.revokeObjectURL(u),10000);
    } catch(err){notify(err.message,true);}
  };
  $("source").onclick=()=>preview(url("/api/source",{id:$("entry").value}));
  $("guide").onclick=()=>preview("/api/guide");
  $("close-preview").onclick=()=>$("preview").close();
  $("start").onclick=()=>{following=true;action("start");};
  $("pause").onclick=()=>action("pause");
  $("queue-list").onclick=e=>{
    const q=e.target.dataset.q, id=e.target.closest("[data-job]")?.dataset.job;
    if(!q||!id)return;
    if(q==="show"){
      jobId=id; following=false; const j=state.jobs.find(x=>x.id===id);
      if(j?.run) selected=j.run;
      refresh(); return;
    }
    if(q==="stop"&&!window.confirm("停止当前训练进程及其子进程？本轮未落盘的进度可能丢失。"))return;
    action(q,id);
  };
  $("log-task").onchange=e=>{jobId=e.target.value;following=false;refresh();};
  $("run-search").oninput=runOptions;
  $("run").onchange=e=>{selected=e.target.value;following=false;refresh();};
  $("follow").onclick=()=>{following=true;refresh();};
  $("open-run").onclick=()=>{if(detail)action("open_run",detail.id);};
  $("tabs").onclick=e=>{
    if(!e.target.dataset.tab)return;
    tab=e.target.dataset.tab;
    $("tabs").querySelectorAll("button").forEach(b=>b.classList.toggle("selected",b.dataset.tab===tab));
    renderDetail();
  };
}
boot();
