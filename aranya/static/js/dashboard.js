/* ---------- utils ---------- */
function esc(s){return (s==null?'':String(s)).replace(/[&<>"]/g,function(c){return ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'})[c];});}

/* CSRF: prefer the av_csrf cookie (set at login, so it stays correct if you
   sign in from another tab) and fall back to the token baked into the page. */
function csrfToken(){
  var m=document.cookie.match(/(?:^|;\s*)av_csrf=([^;]+)/);
  if(m)return decodeURIComponent(m[1]);
  var el=document.querySelector('meta[name="csrf-token"]');
  return el?el.getAttribute('content'):'';
}
function sendJSON(url,method,body){
  return fetch(url,{method:method,headers:{'Content-Type':'application/json',
    'X-CSRF-Token':csrfToken()},body:JSON.stringify(body||{})});
}
function postJSON(url,body){return sendJSON(url,'POST',body);}
function deleteJSON(url,body){return sendJSON(url,'DELETE',body);}
function timeAgo(iso){if(!iso)return'never';var s=Math.round((Date.now()-new Date(iso).getTime())/1000);return s<5?'just now':s<60?(s+'s ago'):s<3600?(Math.floor(s/60)+'m ago'):(Math.floor(s/3600)+'h ago');}
function fmtDate(iso){var d=new Date(iso+'T00:00:00');return d.toLocaleDateString('en-US',{day:'numeric',month:'short'});}
function todayIso(){return new Date().toISOString().slice(0,10);}
function optHtml(val,opts){return opts.map(function(o){return '<option value="'+o[0]+'"'+(val===o[0]?' selected':'')+'>'+o[1]+'</option>';}).join('');}

/* ---------- prefs (persisted) ---------- */
var PREF_KEYS={fltTrek:'all',fltDate:'all',sort:'fav',group:'none',density:'comfortable',theme:'midnight',accent:'#3b82f6',light:'0',font:'inter',fontScale:'1'};
var prefs=Object.assign({},PREF_KEYS);
try{var saved=JSON.parse(localStorage.getItem('aranya_prefs')||'{}');Object.assign(prefs,saved);}catch(e){}
function savePrefs(){localStorage.setItem('aranya_prefs',JSON.stringify(prefs));}

var FONTS={
  inter:"'Inter',system-ui,-apple-system,'Segoe UI',sans-serif",
  outfit:"'Outfit',system-ui,-apple-system,'Segoe UI',sans-serif",
  system:"system-ui,-apple-system,'Segoe UI',Roboto,'Helvetica Neue',Arial,sans-serif"
};
var FONT_LABELS=[['inter','Inter'],['outfit','Outfit'],['system','System']];
var SCALES=[['0.93','S'],['1','M'],['1.08','L'],['1.18','XL']];

var THEMES={
  midnight:{'--bg':'#070a12','--bg2':'#0a0f1a','--surface':'#0e1320','--surface2':'#111827','--border':'#1d293f','--border-hi':'#2c3e5d','--text':'#cfd6e4','--text-dim':'#62738d','--text-bright':'#f1f5f9'},
  slate:{'--bg':'#0b0d10','--bg2':'#101317','--surface':'#15191f','--surface2':'#1b2028','--border':'#262d38','--border-hi':'#38414f','--text':'#d0d6de','--text-dim':'#7c8794','--text-bright':'#f4f6f8'},
  forest:{'--bg':'#060d0a','--bg2':'#0a140f','--surface':'#0d1a13','--surface2':'#112418','--border':'#193626','--border-hi':'#245038','--text':'#c8e0d2','--text-dim':'#6d8a7c','--text-bright':'#effaf3'}
};
function shade(hex,p){var n=parseInt(hex.slice(1),16),r=(n>>16)&255,g=(n>>8)&255,b=n&255;r=Math.max(0,Math.min(255,Math.round(r*(1-p))));g=Math.max(0,Math.min(255,Math.round(g*(1-p))));b=Math.max(0,Math.min(255,Math.round(b*(1-p))));return '#'+((1<<24)+(r<<16)+(g<<8)+b).toString(16).slice(1);}
function applyTheme(){
  var root=document.documentElement;
  document.documentElement.setAttribute('data-light',prefs.light);
  var pal=THEMES[prefs.theme]||THEMES.midnight;
  if(prefs.light!=='1'){for(var k in pal)root.style.setProperty(k,pal[k]);}
  else{['--bg','--bg2','--surface','--surface2','--border','--border-hi','--text','--text-dim','--text-bright'].forEach(function(k){root.style.removeProperty(k);});}
  root.style.setProperty('--accent',prefs.accent);
  root.style.setProperty('--accent-2',shade(prefs.accent,0.18));
  root.style.setProperty('--font',FONTS[prefs.font]||FONTS.inter);
  root.style.setProperty('--fs-scale',prefs.fontScale);
  root.setAttribute('data-density',prefs.density);
}
applyTheme();

/* ---------- global state ---------- */
var latest={rows:[],weekends:[],favourites:[],watch:[],window_days:30};
var catalog=[];
var activeTab='board';

function switchTab(name){
  activeTab=name;
  ['board','calendar','favourites','settings'].forEach(function(n){document.getElementById('tab-'+n).style.display=(n===name?'':'none');});
  document.querySelectorAll('.tab-btn').forEach(function(b){b.classList.toggle('active',b.dataset.tab===name);});
  if(name==='board')renderBoard();
  else if(name==='calendar')renderCalendar();
  else if(name==='favourites')renderFavourites();
  else if(name==='settings')renderSettings();
}

/* ---------- SSE ---------- */
function render(state){
  latest=state;
  var dot=document.getElementById('dot'),txt=document.getElementById('statusTxt');
  if(state.error){dot.className='dot err';txt.textContent=state.error;}
  else{dot.className='dot';txt.textContent='Live · sweep '+state.cycle;}
  document.getElementById('updTxt').textContent='updated '+timeAgo(state.last_update);
  document.getElementById('rangeTxt').textContent=fmtDate(state.window_start)+' – '+fmtDate(state.window_end)+' · next '+state.window_days+'d';
  document.querySelectorAll('#winSeg button').forEach(function(b){b.classList.toggle('on',+b.dataset.d===state.window_days);});
  var f=document.getElementById('flash');f.classList.add('on');setTimeout(function(){f.classList.remove('on');},350);
  if(activeTab==='board')renderBoard();
}

/* ---------- BOARD ---------- */
function cellStatus(iso,cell){
  if(iso<todayIso())return{cls:'st-past',st:'—',nn:'',bar:0};
  if(!cell)return{cls:'st-pend',st:'· · ·',nn:'',bar:0};
  if(!cell.released)return{cls:'st-unrel',st:'unreleased',nn:'',bar:0};
  if(cell.available>0)return{cls:'st-open',st:'open',nn:cell.available+'/'+cell.capacity,bar:cell.capacity?cell.available/cell.capacity:0};
  return{cls:'st-sold',st:'sold out',nn:'0/'+cell.capacity,bar:0};
}
function rowMatches(row){
  if(prefs.fltTrek!=='all'){
    var exists=(latest.favourites||[]).some(function(f){return String(f.trek_id)===String(prefs.fltTrek);});
    if(exists&&String(row.trek_id)!==String(prefs.fltTrek))return false;
  }
  return true;
}
function visibleCols(){
  var wk=latest.weekends||[];
  if(prefs.fltDate!=='all'&&wk.some(function(c){return c.iso===prefs.fltDate;}))
    return wk.filter(function(c){return c.iso===prefs.fltDate;});
  return wk;
}
function visibleIsos(){return visibleCols().map(function(c){return c.iso;});}
function rowOpenTotal(row){var isos=visibleIsos(),t=0;isos.forEach(function(iso){var c=row.cells[iso];if(c&&c.released)t+=c.available;});return t;}
function sortedRows(rows){
  var r=rows.slice();
  if(prefs.sort==='name')r.sort(function(a,b){return a.name.localeCompare(b.name);});
  else if(prefs.sort==='district')r.sort(function(a,b){return (a.district_name||'').localeCompare(b.district_name||'')||a.name.localeCompare(b.name);});
  else if(prefs.sort==='open')r.sort(function(a,b){return rowOpenTotal(b)-rowOpenTotal(a);});
  return r;
}
function toolbarHtml(){
  var trekOpts='<option value="all">All treks</option>'+(latest.favourites||[]).map(function(f){
    return '<option value="'+f.trek_id+'"'+(String(prefs.fltTrek)===String(f.trek_id)?' selected':'')+'>'+esc(f.name)+'</option>';}).join('');
  var dateOpts='<option value="all">All weekends</option>'+(latest.weekends||[]).map(function(c){
    return '<option value="'+c.iso+'"'+(prefs.fltDate===c.iso?' selected':'')+'>'+c.weekday+', '+c.month+' '+c.day+'</option>';}).join('');
  return '<div class="toolbar">'
    +'<div class="tb grow"><label class="fld">Trek</label><select onchange="onPref(\'fltTrek\',this.value)">'+trekOpts+'</select></div>'
    +'<div class="tb grow"><label class="fld">Date</label><select onchange="onPref(\'fltDate\',this.value)">'+dateOpts+'</select></div>'
    +'</div>';
}
function onPref(k,v){prefs[k]=v;savePrefs();if(k==='density')applyTheme();renderBoard();}

function kpiHtml(){
  var isos=visibleIsos();
  var totalOpen=0,treksOpen=0;
  (latest.rows||[]).forEach(function(r){var ro=0;isos.forEach(function(iso){var c=r.cells[iso];if(c&&c.released)ro+=c.available;});totalOpen+=ro;if(ro>0)treksOpen++;});
  return '<div class="summary">'
    +'<span class="s"><b>'+(latest.favourites||[]).length+'</b> treks</span>'
    +'<span class="s"><b>'+isos.length+'</b> weekend dates</span>'
    +'<span class="s '+(totalOpen>0?'good':'')+'"><b>'+totalOpen+'</b> open slots</span>'
    +'<span class="s '+(treksOpen>0?'good':'')+'"><b>'+treksOpen+'</b> with openings</span>'
    +'</div>';
}
function legendHtml(){
  return '<div class="legend">'
    +'<span class="lg"><span class="sw" style="background:var(--green)"></span> Open (available/capacity)</span>'
    +'<span class="lg"><span class="sw" style="background:var(--red)"></span> Sold out</span>'
    +'<span class="lg"><span class="sw" style="background:var(--border-hi)"></span> Unreleased (opens later)</span>'
    +'<span class="lg"><span class="sw" style="background:var(--surface2)"></span> Waiting for first sweep</span>'
    +'</div>';
}
function renderBoard(){
  var host=document.getElementById('tab-board');
  var cols=visibleCols();
  var rows=(latest.rows||[]).filter(rowMatches);
  var head=kpiHtml()+pinnedStripHtml()+toolbarHtml();
  if(!(latest.favourites||[]).length){host.innerHTML=head+'<div class="card"><div class="empty">No favourite treks yet. Add some under the <b>Favourites</b> tab — they appear here across the next '+latest.window_days+' days of weekends.</div></div>';return;}
  if(!cols.length){host.innerHTML=head+'<div class="card"><div class="empty">No weekends in the selected window.</div></div>';return;}
  if(!rows.length){host.innerHTML=head+'<div class="card"><div class="empty">No treks match the current filters.</div></div>';return;}

  // group headers (weekend blocks)
  var groupSpans=[];var lastG=null;
  cols.forEach(function(c){if(c.group!==lastG){groupSpans.push({g:c.group,label:c.month+' '+c.day,count:1,firstIdx:c.iso});lastG=c.group;}else{groupSpans[groupSpans.length-1].count++;}});
  var firstOfGroup={};groupSpans.forEach(function(g){firstOfGroup[g.firstIdx]=1;});

  var gh='<tr class="grp-head"><th class="corner">Trek</th>';
  groupSpans.forEach(function(g){gh+='<th class="gh" colspan="'+g.count+'">Weekend · '+esc(g.label)+'</th>';});
  gh+='</tr>';
  var chh='<tr class="col-head"><th class="corner" style="top:auto"></th>';
  cols.forEach(function(c){chh+='<th class="ch"><div class="dw">'+c.weekday+'</div><div class="dd">'+c.day+'</div></th>';});
  chh+='</tr>';

  rows=sortedRows(rows);
  var body='';
  function emitRow(r){
    var tds='';
    cols.forEach(function(c){
      var s=cellStatus(c.iso,r.cells[c.iso]);
      var cls='cell '+s.cls+(firstOfGroup[c.iso]?' wkend-start':'')+(s.cls==='st-open'?' is-open':'');
      // bar colour grades with how full the slot is: green > 40% > amber > 15% > red
      var lvl=s.bar<0.15?' lo':(s.bar<0.40?' mid':'');
      var inner='<div class="pill '+s.cls+'"><span class="st">'+esc(s.st)+'</span>'+(s.nn?'<span class="nn">'+esc(s.nn)+'</span>':'')+(s.cls==='st-open'?'<span class="bar"><i class="'+lvl.trim()+'" style="width:'+Math.round(s.bar*100)+'%"></i></span>':'')+'</div>';
      tds+='<td class="'+cls+'">'+inner+'</td>';
    });
    return '<tr class="tr-row"><td class="trek-cell"><div class="trek-name">'+esc(r.name)+'</div><div class="trek-dist">'+esc(r.district_name||'')+'</div></td>'+tds+'</tr>';
  }
  if(prefs.group==='district'){
    var byd={};rows.forEach(function(r){(byd[r.district_name||'—']=byd[r.district_name||'—']||[]).push(r);});
    Object.keys(byd).sort().forEach(function(d){
      body+='<tr class="grp-row"><td colspan="'+(cols.length+1)+'">'+esc(d)+'</td></tr>';
      byd[d].forEach(function(r){body+=emitRow(r);});
    });
  }else{rows.forEach(function(r){body+=emitRow(r);});}

  host.innerHTML=head
    +'<div class="board-scroll"><table class="board"><thead>'+gh+chh+'</thead><tbody>'+body+'</tbody></table></div>'
    +legendHtml();
}

/* ---------- FAVOURITES ---------- */
function loadCatalog(cb){
  if(catalog.length){cb&&cb();return;}
  fetch('/api/catalog').then(function(r){return r.json();}).then(function(d){catalog=d.treks||[];cb&&cb();});
}
function renderFavourites(){
  var host=document.getElementById('tab-favourites');
  loadCatalog(function(){
    var favIds={};(latest.favourites||[]).forEach(function(f){favIds[f.trek_id]=1;});
    var g={};catalog.filter(function(t){return !favIds[t.id];}).forEach(function(t){var k=t.district_name||'Other';(g[k]=g[k]||[]).push(t);});
    var opts='<option value="" disabled selected>Choose a trek to feature…</option>';
    Object.keys(g).sort().forEach(function(dist){opts+='<optgroup label="'+esc(dist)+'">';g[dist].forEach(function(t){opts+='<option value="'+t.id+'">'+esc(t.name)+' (#'+t.id+')</option>';});opts+='</optgroup>';});
    var add='<div class="card"><div class="ptitle">Featured treks</div><div class="psub">These are the rows on the board. Reorder to control display order.</div>'
      +'<div class="row"><div style="flex:3"><label class="fld">Add a trek</label><select id="favSel">'+opts+'</select></div>'
      +'<div style="flex:0"><button class="btn" onclick="addFav()">Add to board</button></div></div>'
      +'<div class="msg" id="favMsg"></div></div>';
    var favs=latest.favourites||[];
    var list=favs.map(function(f,i){
      return '<div class="lrow"><div><span class="nm">'+esc(f.name)+'</span> <span class="mt">#'+f.trek_id+' · '+esc(f.district_name||'')+'</span></div>'
        +'<div style="display:flex;gap:6px">'
        +'<button class="btn-sm" '+(i===0?'disabled':'')+' onclick="moveFav('+f.trek_id+',-1)">↑</button>'
        +'<button class="btn-sm" '+(i===favs.length-1?'disabled':'')+' onclick="moveFav('+f.trek_id+',1)">↓</button>'
        +'<button class="btn-sm btn-danger" onclick="delFav('+f.trek_id+')">Remove</button></div></div>';
    }).join('');
    if(!favs.length)list='<div class="empty">No featured treks yet.</div>';
    host.innerHTML=add+'<div class="card">'+list+'</div>';
  });
}
function addFav(){var v=document.getElementById('favSel').value;if(!v)return;
  postJSON('/api/favourites',{trek_id:+v})
  .then(function(r){return r.json();}).then(function(d){if(d.error){document.getElementById('favMsg').textContent=d.error;return;}setTimeout(renderFavourites,300);});}
function delFav(id){deleteJSON('/api/favourites',{trek_id:id}).then(function(){setTimeout(renderFavourites,300);});}
function moveFav(id,dir){
  var ids=(latest.favourites||[]).map(function(f){return f.trek_id;});
  var i=ids.indexOf(id),j=i+dir;if(i<0||j<0||j>=ids.length)return;
  ids.splice(j,0,ids.splice(i,1)[0]);
  postJSON('/api/favourites/reorder',{order:ids}).then(function(){setTimeout(renderFavourites,250);});
}

/* ---------- pinned (stick a trek+date) ---------- */
function fmtDayFull(iso){var d=new Date(iso+'T00:00:00');return d.toLocaleDateString('en-US',{weekday:'short',day:'numeric',month:'short'});}
function pinnedSet(){var s={};(latest.watch||[]).forEach(function(w){s[w.trek_id+'_'+w.date]=1;});return s;}
function pinnedStripHtml(){
  var w=(latest.watch||[]).slice().sort(function(a,b){return a.date.localeCompare(b.date);});
  if(!w.length)return '';
  var chips=w.map(function(x){
    var c=x.cell,cls='pchip',v='soon';
    if(c){if(!c.released)v='soon';else if(c.available>0){cls+=' open';v=c.available+'/'+c.capacity;}else{cls+=' sold';v='sold';}}
    else v='…';
    return '<span class="'+cls+'">'+esc(x.name)+' · '+fmtDayFull(x.date)+' <b>'+v+'</b>'
      +'<span class="x" title="Unpin" onclick="togglePin('+x.trek_id+',\''+x.date+'\',1)">✕</span></span>';
  }).join('');
  return '<div class="pinned-strip"><span class="ps-lbl">📌 Pinned</span>'+chips+'</div>';
}
function togglePin(tid,iso,isPinned){
  sendJSON('/api/watch',isPinned?'DELETE':'POST',{trek_id:tid,date:iso})
    .then(function(){setTimeout(function(){if(activeTab==='calendar')renderCalendar();else renderBoard();},300);});
}

/* ---------- CALENDAR (any trek, any date) ---------- */
var calState={trek:null,ym:null};
function renderCalendar(){
  var host=document.getElementById('tab-calendar');
  loadCatalog(function(){
    if(calState.trek==null){var f=(latest.favourites||[])[0];calState.trek=f?f.trek_id:((catalog[0]||{}).id||null);}
    if(!calState.ym){var d=new Date();calState.ym={y:d.getFullYear(),m:d.getMonth()+1};}
    if(calState.trek==null){host.innerHTML='<div class="card"><div class="empty">No treks available yet — mapping the portal…</div></div>';return;}
    var g={};catalog.forEach(function(t){var k=t.district_name||'Other';(g[k]=g[k]||[]).push(t);});
    var opts='';Object.keys(g).sort().forEach(function(dist){opts+='<optgroup label="'+esc(dist)+'">';
      g[dist].forEach(function(t){opts+='<option value="'+t.id+'"'+(String(calState.trek)===String(t.id)?' selected':'')+'>'+esc(t.name)+' (#'+t.id+')</option>';});opts+='</optgroup>';});
    var mlabel=new Date(calState.ym.y,calState.ym.m-1,1).toLocaleDateString('en-US',{month:'long',year:'numeric'});
    host.innerHTML='<div class="card">'+pinnedStripHtml()
      +'<div class="cal-top"><div class="tb grow" style="max-width:340px"><label class="fld">Trek</label><select onchange="calPick(this.value)">'+opts+'</select></div>'
      +'<div class="cal-nav"><button class="btn-sm" onclick="calMonth(-1)">‹</button><span class="mlabel">'+esc(mlabel)+'</span><button class="btn-sm" onclick="calMonth(1)">›</button></div></div>'
      +'<div id="calBody"><div class="empty">Checking availability…</div></div>'
      +'<div class="cal-hint">Tap any day to pin that trek + date. Pinned combos stay live at the top here and on the Weekends board.</div></div>';
    var mm=calState.ym.y+'-'+String(calState.ym.m).padStart(2,'0');
    fetch('/api/trek-calendar?trek_id='+calState.trek+'&month='+mm)
      .then(function(r){return r.json();}).then(renderCalBody)
      .catch(function(){var b=document.getElementById('calBody');if(b)b.innerHTML='<div class="empty">Could not load availability.</div>';});
  });
}
function renderCalBody(d){
  var body=document.getElementById('calBody');if(!body)return;
  if(d.error){body.innerHTML='<div class="empty">'+esc(d.error)+'</div>';return;}
  var pins=pinnedSet();
  var dow=['Sun','Mon','Tue','Wed','Thu','Fri','Sat'];
  var html='<div class="cal-grid">'+dow.map(function(x){return '<div class="cal-dow">'+x+'</div>';}).join('');
  var firstDow=new Date(d.days[0].iso+'T00:00:00').getDay();
  for(var i=0;i<firstDow;i++)html+='<div class="cal-day blank"></div>';
  d.days.forEach(function(day){
    var c=day.cell,cls='cal-day',cs='<span class="cs">—</span>';
    if(day.past){cls+=' past';cs='';}
    else if(!c){cs='<span class="cs">…</span>';}
    else if(!c.released){cs='<span class="cs">soon</span>';}
    else if(c.available>0){cls+=' c-open';cs='<span class="cs">'+c.available+'/'+c.capacity+'</span>';}
    else{cls+=' c-sold';cs='<span class="cs">sold</span>';}
    var key=d.trek.trek_id+'_'+day.iso,isPin=!!pins[key];
    if(isPin)cls+=' pinned';
    var click=day.past?'':' onclick="togglePin('+d.trek.trek_id+',\''+day.iso+'\','+(isPin?1:0)+')"';
    html+='<div class="'+cls+'"'+click+'>'+(isPin?'<span class="cal-pin">📌</span>':'')+'<span class="dn">'+day.day+'</span>'+cs+'</div>';
  });
  html+='</div>';
  body.innerHTML=html;
}
function calPick(v){calState.trek=+v;renderCalendar();}
function calMonth(dir){var m=calState.ym.m+dir,y=calState.ym.y;if(m<1){m=12;y--;}if(m>12){m=1;y++;}calState.ym={y:y,m:m};renderCalendar();}

/* ---------- SETTINGS ---------- */
function renderSettings(){
  var host=document.getElementById('tab-settings');
  var themeSw=Object.keys(THEMES).map(function(t){return '<div class="swatch'+(prefs.theme===t?' on':'')+'" title="'+t+'" style="background:linear-gradient(135deg,'+THEMES[t]['--surface']+','+THEMES[t]['--bg']+')" onclick="setTheme(\''+t+'\')"></div>';}).join('');
  var accents=['#3b82f6','#22c55e','#f59e0b','#ef4444','#a855f7','#06b6d4','#ec4899'];
  var accSw=accents.map(function(a){return '<div class="swatch'+(prefs.accent===a?' on':'')+'" style="background:'+a+'" onclick="setAccent(\''+a+'\')"></div>';}).join('');
  // each typeface button previews itself, so the choice is visible before committing
  var fontSeg=FONT_LABELS.map(function(f){
    return '<button class="'+(prefs.font===f[0]?'on':'')+'" style="font-family:'+FONTS[f[0]]+'" onclick="setFont(\''+f[0]+'\')">'+f[1]+'</button>';
  }).join('');
  var scaleSeg=SCALES.map(function(s){
    return '<button class="'+(prefs.fontScale===s[0]?'on':'')+'" onclick="setFontScale(\''+s[0]+'\')">'+s[1]+'</button>';
  }).join('');
  host.innerHTML=
    '<div class="card"><div class="ptitle">Appearance</div><div class="psub">Saved in this browser.</div>'
    +'<label class="fld">Dark palette</label><div class="swatches" style="margin-bottom:14px">'+themeSw+'</div>'
    +'<label class="fld">Accent</label><div class="swatches" style="margin-bottom:14px">'+accSw
      +'<input type="color" value="'+prefs.accent+'" onchange="setAccent(this.value)" style="width:34px;height:30px;padding:0;border:none;background:none;cursor:pointer"></div>'
    +'<label class="fld">Mode</label><div class="seg" style="margin-bottom:14px"><button class="'+(prefs.light!=='1'?'on':'')+'" onclick="setLight(\'0\')">Dark</button><button class="'+(prefs.light==='1'?'on':'')+'" onclick="setLight(\'1\')">Light</button></div>'
    +'<label class="fld">Typeface</label><div class="seg" style="margin-bottom:14px">'+fontSeg+'</div>'
    +'<label class="fld">Text size</label><div class="seg">'+scaleSeg+'</div>'
    +'</div>'
    +'<div class="card"><div class="ptitle">Board layout</div><div class="psub">How treks are ordered, compared and spaced. Saved in this browser.</div>'
    +'<div class="row">'
    +'<div><label class="fld">Order / compare treks by</label><select onchange="onPref(\'sort\',this.value)">'+optHtml(prefs.sort,[['fav','Favourite order'],['name','Name (A–Z)'],['district','District'],['open','Most availability first']])+'</select></div>'
    +'<div><label class="fld">Group</label><select onchange="onPref(\'group\',this.value)">'+optHtml(prefs.group,[['none','No grouping'],['district','By district']])+'</select></div>'
    +'<div><label class="fld">Row density</label><select onchange="onPref(\'density\',this.value)">'+optHtml(prefs.density,[['comfortable','Comfortable'],['compact','Compact']])+'</select></div>'
    +'</div></div>'
    +'<div class="card"><div class="ptitle">Data</div><div class="psub">Applies to everyone viewing this dashboard.</div>'
    +'<div class="row"><div><label class="fld">Window (days ahead)</label><input type="number" id="setWin" min="1" max="60" value="'+latest.window_days+'"></div>'
    +'<div><label class="fld">Sweep interval (seconds)</label><input type="number" id="setCad" min="5" max="600" value="'+(latest.cadence||40)+'"></div>'
    +'<div style="flex:0"><button class="btn" onclick="saveServerSettings()">Apply</button></div></div>'
    +'<div class="msg" id="setMsg" style="color:var(--green)"></div></div>';
}
function setTheme(t){prefs.theme=t;savePrefs();applyTheme();renderSettings();}
function setAccent(a){prefs.accent=a;savePrefs();applyTheme();renderSettings();}
function setLight(v){prefs.light=v;savePrefs();applyTheme();renderSettings();}
function setFont(f){prefs.font=f;savePrefs();applyTheme();renderSettings();}
function setFontScale(s){prefs.fontScale=s;savePrefs();applyTheme();renderSettings();}
function saveServerSettings(){
  var win=+document.getElementById('setWin').value,cad=+document.getElementById('setCad').value;
  postJSON('/api/settings',{window_days:win,cadence:cad})
  .then(function(r){return r.json();}).then(function(){document.getElementById('setMsg').textContent='Saved. Board updates on the next sweep.';});
}

/* ---------- window segmented control ---------- */
document.getElementById('winSeg').addEventListener('click',function(e){
  var b=e.target.closest('button');if(!b)return;
  postJSON('/api/settings',{window_days:+b.dataset.d});
});

/* ---------- live link ---------- */
var lastEventAt=0;
function connectStream(){var es=new EventSource('/api/stream');
  es.onmessage=function(ev){lastEventAt=Date.now();try{render(JSON.parse(ev.data));}catch(e){}};
  es.onerror=function(){document.getElementById('dot').className='dot dead';};}
function pollFallback(){if(Date.now()-lastEventAt>20000){fetch('/api/state').then(function(r){return r.json();}).then(render).catch(function(){var d=document.getElementById('dot');d.className='dot dead';document.getElementById('statusTxt').textContent='Server unreachable';});}}
connectStream();setInterval(pollFallback,5000);
fetch('/api/state').then(function(r){return r.json();}).then(render);
