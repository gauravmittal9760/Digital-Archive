(function(){'use strict';
var $=function(s,r){return(r||document).querySelector(s)},$$=function(s,r){return Array.prototype.slice.call((r||document).querySelectorAll(s))};
var reduce=matchMedia('(prefers-reduced-motion: reduce)').matches,fine=matchMedia('(hover:hover) and (pointer:fine)').matches;
/* particle network */
var cv=$('#fx');
if(cv&&!reduce){var x=cv.getContext('2d'),W,H,P=[],mx=-999,my=-999;
  function size(){W=cv.width=innerWidth;H=cv.height=innerHeight;P=[];var n=Math.min(70,Math.floor(W*H/22000));for(var i=0;i<n;i++)P.push({x:Math.random()*W,y:Math.random()*H,vx:(Math.random()-.5)*.35,vy:(Math.random()-.5)*.35,r:Math.random()*1.6+.6})}
  addEventListener('resize',size);size();addEventListener('mousemove',function(e){mx=e.clientX;my=e.clientY});
  (function loop(){x.clearRect(0,0,W,H);
    for(var i=0;i<P.length;i++){var p=P[i];p.x+=p.vx;p.y+=p.vy;if(p.x<0||p.x>W)p.vx*=-1;if(p.y<0||p.y>H)p.vy*=-1;
      var dx=p.x-mx,dy=p.y-my,d=Math.sqrt(dx*dx+dy*dy);if(d<140){p.x+=dx/d*1.2;p.y+=dy/d*1.2}
      x.beginPath();x.arc(p.x,p.y,p.r,0,6.283);x.fillStyle='rgba(150,170,255,.7)';x.fill();
      for(var j=i+1;j<P.length;j++){var q=P[j],ax=p.x-q.x,ay=p.y-q.y,dd=ax*ax+ay*ay;if(dd<14000){x.strokeStyle='rgba(124,92,255,'+(.22*(1-dd/14000))+')';x.beginPath();x.moveTo(p.x,p.y);x.lineTo(q.x,q.y);x.stroke()}}}
    requestAnimationFrame(loop)})()}
/* custom cursor */
if(fine&&!reduce){document.body.classList.add('has-cursor');
  var dot=document.createElement('div'),ring=document.createElement('div');dot.className='cur-dot';ring.className='cur-ring';document.body.append(dot,ring);
  var tx=0,ty=0,rx=0,ry=0,last=0;
  addEventListener('mousemove',function(e){tx=e.clientX;ty=e.clientY;dot.style.transform='translate('+tx+'px,'+ty+'px)';
    var n=Date.now();if(n-last>45){last=n;var t=document.createElement('div');t.className='trail';t.style.left=tx-3+'px';t.style.top=ty-3+'px';document.body.appendChild(t);setTimeout(function(){t.remove()},700)}});
  (function f(){rx+=(tx-rx)*.16;ry+=(ty-ry)*.16;ring.style.transform='translate('+rx+'px,'+ry+'px)';requestAnimationFrame(f)})();
  document.addEventListener('mouseover',function(e){if(e.target.closest('a,button,.opt,.tile,input,select,textarea,label,tr[data-href]'))ring.classList.add('hover')});
  document.addEventListener('mouseout',function(e){if(e.target.closest('a,button,.opt,.tile,input,select,textarea,label,tr[data-href]'))ring.classList.remove('hover')});
  addEventListener('mousedown',function(){ring.classList.add('down')});addEventListener('mouseup',function(){ring.classList.remove('down')});
  /* magnetic buttons */
  $$('.btn,.opt').forEach(function(b){b.addEventListener('mousemove',function(e){var r=b.getBoundingClientRect();b.style.translate=((e.clientX-r.left-r.width/2)*.12)+'px '+((e.clientY-r.top-r.height/2)*.18)+'px'});b.addEventListener('mouseleave',function(){b.style.translate=''})})}
/* spotlight cards + tilt */
$$('.card').forEach(function(c){c.addEventListener('mousemove',function(e){var r=c.getBoundingClientRect();c.style.setProperty('--mx',(e.clientX-r.left)+'px');c.style.setProperty('--my',(e.clientY-r.top)+'px');
  if(c.dataset.tilt!==undefined&&!reduce){c.style.transform='perspective(800px) rotateX('+(-(e.clientY-r.top-r.height/2)/r.height*6)+'deg) rotateY('+((e.clientX-r.left-r.width/2)/r.width*6)+'deg)'}});c.addEventListener('mouseleave',function(){c.style.transform=''})});
/* rotating avatars */
$$('[data-rotate]').forEach(function(box){var imgs=$$('img',box);if(!imgs.length)return;var i=0;imgs[0].classList.add('on');var ph=$('.ph',box);ph&&(ph.style.display='none');
  if(imgs.length>1)setInterval(function(){imgs[i].classList.remove('on');i=(i+1)%imgs.length;imgs[i].classList.add('on')},3800)});
/* count-up + bars */
$$('[data-count]').forEach(function(el){var to=parseFloat(el.dataset.count),t0=null;function s(ts){t0=t0||ts;var p=Math.min(1,(ts-t0)/1100);el.textContent=Math.round(to*(1-Math.pow(1-p,3))).toLocaleString();if(p<1)requestAnimationFrame(s)}requestAnimationFrame(s)});
requestAnimationFrame(function(){$$('.bar>i[data-w]').forEach(function(i){i.style.setProperty('--w',i.dataset.w+'%')})});
/* signup bar chart */
$$('[data-chart]').forEach(function(el){var d=JSON.parse(el.dataset.chart),max=Math.max.apply(null,d.map(function(a){return a[1]}).concat([1])),w=600,h=190,bw=w/d.length;
  var s='<svg class="chart" viewBox="0 0 '+w+' '+h+'" preserveAspectRatio="none"><defs><linearGradient id="g1" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#00E5FF"/><stop offset="1" stop-color="#7C5CFF"/></linearGradient></defs>';
  d.forEach(function(a,i){var bh=Math.max(3,(a[1]/max)*(h-34));s+='<rect x="'+(i*bw+2)+'" y="'+(h-20-bh)+'" width="'+(bw-4)+'" height="'+bh+'" rx="3"><title>'+a[0]+': '+a[1]+'</title></rect>';if(i%5===0)s+='<text x="'+(i*bw)+'" y="'+(h-5)+'">'+a[0]+'</text>'});
  el.innerHTML=s+'</svg>'});
/* toasts + confirm + row links */
$$('.toast').forEach(function(t,i){setTimeout(function(){t.style.transition='.4s';t.style.opacity=0;setTimeout(function(){t.remove()},400)},6000+i*500)});
$$('form[data-confirm]').forEach(function(f){f.addEventListener('submit',function(e){if(!confirm(f.dataset.confirm))e.preventDefault()})});
$$('tr[data-href]').forEach(function(r){r.style.cursor='pointer';r.addEventListener('click',function(e){if(!e.target.closest('a,button,form'))location.href=r.dataset.href})});
$$('[data-countdown]').forEach(function(el){var n=parseInt(el.dataset.countdown,10)||0,b=$(el.dataset.target);(function t(){if(n>0){el.textContent='Resend available in '+n+'s';b&&(b.disabled=true);n--;setTimeout(t,1000)}else{el.textContent='';b&&(b.disabled=false)}})()});
$$('[data-pw-toggle]').forEach(function(b){b.addEventListener('click',function(){var i=b.previousElementSibling;i.type=i.type==='password'?'text':'password'})});
$$('.otp').forEach(function(i){i.addEventListener('input',function(){i.value=i.value.replace(/\D/g,'').slice(0,6)})});
/* live stats polling */
var live=$('[data-live]');
if(live){setInterval(function(){fetch(live.dataset.live,{credentials:'same-origin'}).then(function(r){return r.json()}).then(function(j){Object.keys(j).forEach(function(k){var e=$('[data-k="'+k+'"]');if(e&&e.textContent!==String(j[k]))e.textContent=j[k]})}).catch(function(){})},15000)}
/* command palette */
var pal=$('#cmdk');
if(pal){var inp=$('input',pal),links=$$('a',pal);
  function open(){pal.classList.add('open');inp.value='';filter();inp.focus()}function close(){pal.classList.remove('open')}
  function filter(){var q=inp.value.toLowerCase();links.forEach(function(a){a.classList.toggle('hide',a.textContent.toLowerCase().indexOf(q)<0)})}
  addEventListener('keydown',function(e){if((e.ctrlKey||e.metaKey)&&e.key.toLowerCase()==='k'){e.preventDefault();pal.classList.contains('open')?close():open()}if(e.key==='Escape')close();
    if(e.key==='Enter'&&pal.classList.contains('open')){var a=links.filter(function(a){return!a.classList.contains('hide')})[0];a&&(location.href=a.href)}});
  inp.addEventListener('input',filter);pal.addEventListener('click',function(e){if(e.target===pal)close()});var op=$('[data-cmdk]');op&&op.addEventListener('click',open)}
})();
