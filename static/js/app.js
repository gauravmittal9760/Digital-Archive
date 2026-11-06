/* Digital Archive — client behaviour */
(function(){
'use strict';
var $=function(s,r){return (r||document).querySelector(s)},$$=function(s,r){return Array.prototype.slice.call((r||document).querySelectorAll(s))};
var csrf=(window.DA&&DA.csrf)||($('meta[name=csrf-token]')||{}).content||'';

/* theme: icon-only toggle, saved to the account (or cookie when signed out) */
var tt=$('#themeToggle');
if(tt)tt.addEventListener('click',function(){
  var next=document.documentElement.dataset.theme==='dark'?'light':'dark';
  document.documentElement.dataset.theme=next;
  try{localStorage.setItem('da_theme',next)}catch(e){}
  fetch(DA.themeUrl,{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':csrf},body:JSON.stringify({theme:next}),credentials:'same-origin'}).catch(function(){});
});

/* back buttons: go to the previous page when it is on this site, else the safe fallback link */
$$('[data-back]').forEach(function(a){a.addEventListener('click',function(e){
  try{if(history.length>1&&document.referrer&&new URL(document.referrer).origin===location.origin&&new URL(document.referrer).pathname!==location.pathname){e.preventDefault();history.back()}}catch(x){}
})});

/* password show/hide */
$$('[data-pw-toggle]').forEach(function(b){b.addEventListener('click',function(){
  var i=b.parentNode.querySelector('input');i.type=i.type==='password'?'text':'password';b.style.color=i.type==='text'?'var(--brand)':'';
})});

/* menus + toasts */
$$('[data-menu]').forEach(function(b){b.addEventListener('click',function(e){e.stopPropagation();b.parentNode.classList.toggle('open')})});
document.addEventListener('click',function(e){$$('.menu.open').forEach(function(m){if(!m.contains(e.target))m.classList.remove('open')});
  var c=e.target.closest&&e.target.closest('[data-close]');if(c){var t=c.closest('.toast');t&&t.remove()}});
$$('.toast').forEach(function(t,i){setTimeout(function(){t.style.transition='.4s';t.style.opacity='0';t.style.transform='translateX(20px)';setTimeout(function(){t.remove()},400)},6500+i*600)});

/* scroll-to-top */
var top=$('#toTop');
if(top){addEventListener('scroll',function(){top.classList.toggle('show',scrollY>500)},{passive:true});top.addEventListener('click',function(){scrollTo({top:0,behavior:'smooth'})})}

/* confirm dialogs on destructive forms */
$$('form[data-confirm]').forEach(function(f){f.addEventListener('submit',function(e){if(!confirm(f.dataset.confirm))e.preventDefault()})});

/* OTP resend countdown */
$$('[data-countdown]').forEach(function(el){
  var left=parseInt(el.dataset.countdown,10)||0,btn=$(el.dataset.target||'[data-resend]');
  function tick(){if(left>0){el.textContent='You can request a new code in '+left+'s';btn&&(btn.disabled=true);left--;setTimeout(tick,1000)}else{el.textContent='';btn&&(btn.disabled=false)}}
  tick();
});
$$('.otp-input').forEach(function(i){i.addEventListener('input',function(){i.value=i.value.replace(/\D/g,'').slice(0,6);if(i.value.length===6&&i.form&&i.dataset.auto!=='off')i.form.requestSubmit?i.form.requestSubmit():i.form.submit()})});

/* animated meters */
requestAnimationFrame(function(){$$('.meter>i[data-w]').forEach(function(i){i.style.setProperty('--w',i.dataset.w+'%')})});
$$('[data-count]').forEach(function(el){var to=parseFloat(el.dataset.count),d=900,t0=null,dec=(el.dataset.count.split('.')[1]||'').length;
  function s(ts){t0=t0||ts;var p=Math.min(1,(ts-t0)/d);el.textContent=(to*(1-Math.pow(1-p,3))).toFixed(dec);if(p<1)requestAnimationFrame(s)}requestAnimationFrame(s)});

/* gallery selection + bulk actions */
var grid=$('[data-selectable]');
if(grid){
  var bar=$('#bulkbar'),cnt=$('#bulkCount'),form=$('#bulkForm');
  function refresh(){var ids=$$('.m-pick input:checked',grid);ids.length?bar.classList.add('show'):bar.classList.remove('show');cnt.textContent=ids.length+' selected';
    $$('.m-card',grid).forEach(function(c){c.classList.toggle('picked',!!$('.m-pick input:checked',c))});
    grid.classList.toggle('select-mode',ids.length>0)}
  grid.addEventListener('change',function(e){if(e.target.matches('.m-pick input'))refresh()});
  var all=$('#bulkAll');all&&all.addEventListener('click',function(){var boxes=$$('.m-pick input',grid),on=boxes.some(function(b){return!b.checked});boxes.forEach(function(b){b.checked=on});refresh()});
  var clear=$('#bulkClear');clear&&clear.addEventListener('click',function(){$$('.m-pick input',grid).forEach(function(b){b.checked=false});refresh()});
  $$('[data-bulk]',bar).forEach(function(b){b.addEventListener('click',function(){
    var ids=$$('.m-pick input:checked',grid).map(function(i){return i.value});if(!ids.length)return;
    if(b.dataset.confirm&&!confirm(b.dataset.confirm))return;
    form.querySelectorAll('input[name=media_ids]').forEach(function(n){n.remove()});
    ids.forEach(function(id){var h=document.createElement('input');h.type='hidden';h.name='media_ids';h.value=id;form.appendChild(h)});
    form.elements.action.value=b.dataset.bulk;form.submit()})});
  $$('.m-fav',grid).forEach(function(b){b.addEventListener('click',function(e){e.preventDefault();e.stopPropagation();
    fetch(b.dataset.url,{method:'POST',headers:{'X-CSRF-Token':csrf,'X-Requested-With':'fetch'},credentials:'same-origin'}).then(function(r){return r.json()}).then(function(j){b.classList.toggle('on',!!j.favorite)})})});
}

/* upload queue: one request per file so progress and errors are per file */
var drop=$('#dropzone');
if(drop){
  var input=$('#fileInput'),queue=$('#queue'),go=$('#startUpload'),clearQ=$('#clearQueue'),files=[],running=false,url=drop.dataset.url,summary=$('#uploadSummary');
  function fmt(n){return n>1048576?(n/1048576).toFixed(1)+' MB':Math.max(1,Math.round(n/1024))+' KB'}
  function add(list){Array.prototype.forEach.call(list,function(f){
    var row=document.createElement('div');row.className='q-item';
    row.innerHTML='<div class="q-ph"></div><div class="grow"><b></b><div class="small muted"></div><div class="meter"><i></i></div></div><span class="q-state">Ready</span>';
    row.querySelector('b').textContent=f.name;row.querySelector('.small').textContent=fmt(f.size);
    if(f.type.indexOf('image/')===0){var im=new Image();im.src=URL.createObjectURL(f);row.replaceChild(im,row.firstChild)}
    queue.appendChild(row);files.push({file:f,row:row,done:false})});
    go.disabled=!files.length;clearQ.classList.toggle('hide',!files.length)}
  drop.addEventListener('click',function(){input.click()});
  input.addEventListener('change',function(){add(input.files);input.value=''});
  ['dragenter','dragover'].forEach(function(n){drop.addEventListener(n,function(e){e.preventDefault();drop.classList.add('over')})});
  ['dragleave','drop'].forEach(function(n){drop.addEventListener(n,function(e){e.preventDefault();drop.classList.remove('over')})});
  drop.addEventListener('drop',function(e){add(e.dataTransfer.files)});
  clearQ.addEventListener('click',function(){if(running)return;files=[];queue.innerHTML='';go.disabled=true;clearQ.classList.add('hide');summary.textContent=''});
  function send(item){return new Promise(function(res){
    var fd=new FormData();fd.append('file',item.file);fd.append('csrf_token',csrf);
    var x=new XMLHttpRequest(),st=item.row.querySelector('.q-state'),bar=item.row.querySelector('.meter>i');
    x.open('POST',url);x.setRequestHeader('X-CSRF-Token',csrf);
    x.upload.onprogress=function(e){if(e.lengthComputable){bar.style.setProperty('--w',Math.round(e.loaded/e.total*95)+'%');st.textContent=Math.round(e.loaded/e.total*100)+'%'}};
    x.onload=function(){var j={};try{j=JSON.parse(x.responseText)}catch(e){}
      if(x.status<300&&j.status==='ok'){bar.style.setProperty('--w','100%');st.textContent=(j.category||'Saved')+(j.near_duplicate?' · duplicate':'');st.className='q-state ok';item.ok=true;item.row.title=j.message||''}
      else if(j.status==='duplicate'||j.status==='skipped'){bar.style.setProperty('--w','100%');st.textContent='Duplicate skipped';st.className='q-state';item.row.title=j.message||''}
      else{st.textContent=j.message||j.error||'Failed';st.className='q-state err';bar.style.setProperty('--w','100%');bar.style.background='var(--err)'}
      item.done=true;res()};
    x.onerror=function(){st.textContent='Network error';st.className='q-state err';item.done=true;res()};
    st.textContent='Uploading…';x.send(fd)})}
  go.addEventListener('click',function(){
    if(running)return;running=true;go.disabled=true;var pending=files.filter(function(f){return!f.done});
    var i=0,workers=Math.min(2,pending.length),finished=0;
    function next(){if(i>=pending.length){if(--workers===0){running=false;var ok=files.filter(function(f){return f.ok}).length;summary.innerHTML='<b>'+ok+' of '+files.length+' uploaded.</b> <a href="'+drop.dataset.gallery+'">Open your library →</a>'}return}
      send(pending[i++]).then(next)}
    for(var w=0;w<Math.min(2,pending.length);w++)next()});
}

/* AI status polling for cards that are still being analysed */
var pend=$$('[data-ai-pending]');
if(pend.length&&window.DA&&DA.statusUrl){var tries=0,iv=setInterval(function(){
  if(++tries>25||!pend.length)return clearInterval(iv);
  var ids=pend.map(function(p){return p.dataset.aiPending}).join(',');
  fetch(DA.statusUrl+'?ids='+encodeURIComponent(ids),{credentials:'same-origin'}).then(function(r){return r.json()}).then(function(j){
    Object.keys(j).forEach(function(id){var it=j[id];if(it.status!=='pending'){var el=$('[data-ai-pending="'+id+'"]');if(el){var lab=$('.m-badge span',el),d=$('.m-badge .dot',el);if(lab)lab.textContent=it.label;if(d)d.style.setProperty('--c',it.color);el.removeAttribute('data-ai-pending')}}});
    pend=$$('[data-ai-pending]')}).catch(function(){})},4000)}
})();
