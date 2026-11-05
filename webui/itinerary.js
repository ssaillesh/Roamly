/* Rova itinerary — design screens 4c (desktop) and 4d (phone).
   Shared by the planner (rova.html: editable) and the public plan page
   (n.html: read-only). Styles live in itinerary.css.

   RovaItin.render(root, plan, o)   draw a plan into root (replaces its content)
   RovaItin.status(root, o)         a loading / error screen in the same frame
   RovaItin.focus(i)                scroll to a stop and light it on the route
   RovaItin.splitTitle(t)           'Tapas, jazz and a last drink' → ['Tapas, jazz', 'and a last drink']

   o = { title, kicker, ctxHtml, intro, fits:[{ok,t}], tweaks:[label], also:[{name, sub, img}],
         readOnly, saved, transport, days:[plan…] (a multi-day trip), coming:true (show "coming soon" bits),
         on:{ close, save, share, settings, swap(i), remove(i), tweak(i), again, surprise, ask(text), also(i), lateBite } }
   Everything shown is computed from the plan — nothing is invented. */
window.RovaItin = (() => {
  const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g, c => ({ '&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;' }[c]));
  const ic = (n, cls = '') => `<i class="ph ph-${n}${cls ? ' ' + cls : ''}" aria-hidden="true"></i>`;
  const safeUrl = u => (typeof u === 'string' && /^https?:\/\//i.test(u.trim())) ? u.trim() : null;
  const pad = n => String(n).padStart(2, '0');
  const cap = s => s ? s.charAt(0).toUpperCase() + s.slice(1) : s;
  function parseClock(s){
    const m = String(s || '').trim().match(/^(\d{1,2})(?::(\d{2}))?(?::\d{2})?\s*([ap]\.?m\.?)?$/i);
    if (!m) return null;
    let h = +m[1]; const mi = +(m[2] || 0);
    if (m[3]){ const pm = /p/i.test(m[3]); if (h === 12) h = 0; if (pm) h += 12; }
    return h < 24 && mi < 60 ? h * 60 + mi : null;
  }
  const short = min => { min = ((Math.round(min) % 1440) + 1440) % 1440; const h = Math.floor(min / 60), m = min % 60; return `${h % 12 || 12}:${pad(m)}`; };
  const rel = min => min >= 360 ? min - 1080 : min + 360;       // clock minutes → minutes after 6 PM
  const legKm = (a, b) => {
    if (!a || !b || a.lat == null || b.lat == null) return null;
    const R = 6371, r = x => x * Math.PI / 180, dLat = r(b.lat - a.lat), dLng = r(b.lng - a.lng);
    return 2 * R * Math.asin(Math.sqrt(Math.sin(dLat / 2) ** 2 + Math.cos(r(a.lat)) * Math.cos(r(b.lat)) * Math.sin(dLng / 2) ** 2)) * 1.25;   // streets, not a straight line
  };
  const walkMin = km => Math.max(1, Math.round(km * 12.5));
  const money = n => '$' + Math.round(n || 0);

  const SLOT = { dinner:['fork-knife','Dinner'], lunch:['fork-knife','Lunch'], cafe:['coffee','Café'], activity:['star','Activity'],
    scenic:['tree','Outdoors'], leisure:['music-notes','Night out'], drinks:['martini','Drinks'], dessert:['ice-cream','Dessert'], event:['ticket','Live event'] };
  const slotOf = s => SLOT[s.slot] || ['map-pin', ''];

  function splitTitle(title){
    const t = String(title || '').split(/\s+/).filter(Boolean).join(' ');
    const words = t.split(' ');
    if (words.length < 2) return [t, ''];
    for (const kw of [' and ', ' with ', ' by ', ' in ', ' at ']){
      const i = t.lastIndexOf(kw); if (i > 0) return [t.slice(0, i), t.slice(i + 1)];
    }
    const k = words.length <= 3 ? 1 : 2;
    return [words.slice(0, -k).join(' '), words.slice(-k).join(' ')];
  }
  const titleHtml = t => { const [a, b] = splitTitle(t); return `${esc(a)}${b ? ` <em>${esc(b)}</em>` : ''}`; };

  /* The night's numbers, from the stops themselves. */
  function stats(plans, transport){
    const all = plans.flatMap(p => p.stops || []);
    const ts = all.map(s => parseClock(s.time)).filter(x => x != null);
    const first = ts.length ? ts[0] : null, last = ts.length ? ts[ts.length - 1] : null;
    const end = last != null ? last + 60 : null;                       // about an hour at the last stop
    const mins = first != null ? rel(end) - rel(first) : null;
    let km = 0, known = false;
    plans.forEach(p => (p.stops || []).forEach((s, i, a) => { const k = legKm(s, a[i + 1]); if (k != null){ km += k; known = true; } }));
    const walkKm = plans.length === 1 && plans[0].walk_km != null ? +plans[0].walk_km : known ? Math.round(km * 10) / 10 : null;
    const pp = Math.max(1, plans[0].party_size || 1);
    const total = plans.reduce((a, p) => a + (p.estimated_cost || 0), 0);
    return {
      span: first != null ? `${short(first)}–${short(end)}` : '', first, end,
      dur: mins > 0 ? `${Math.floor(mins / 60)}h${mins % 60 ? ' ' + pad(mins % 60) : ''}` : '',
      km: walkKm, onFoot: transport ? transport === 'walk' : (walkKm != null && walkKm < 3),
      each: total / pp, total, pp, n: all.length,
    };
  }
  const nWord = n => ['No stops','One stop','Two stops','Three stops','Four stops','Five stops','Six stops'][n] || `${n} stops`;

  function photo(s, cls, eager){
    const img = safeUrl(s && s.image);
    return `<span class="${cls}"><span class="it-noimg">${ic(slotOf(s || {})[0])}</span>${img
      ? `<img src="${esc(img)}" alt="" loading="${eager ? 'eager' : 'lazy'}" decoding="async" referrerpolicy="no-referrer" onerror="this.remove()">` : ''}</span>`;
  }

  function stopHtml(s, i, ctx){
    const [ico, slotName] = slotOf(s);
    const cat = s.categories && s.categories[0] ? cap(String(s.categories[0]).replace(/_/g, ' ')) : '';
    const kicker = [s.label && s.label !== cat ? s.label : slotName, cat].filter(Boolean).filter((x, j, a) => a.indexOf(x) === j).join(' · ');
    const t = parseClock(s.time);
    const note = i === 0 ? 'Arrive' : i === ctx.count - 1 ? 'Last stop' : (s.slot === 'event' ? 'Starts' : '');
    const each = s.est_cost != null ? (s.est_cost === 0 ? 'Free' : `About ${money(s.est_cost)}${ctx.pp > 1 ? ' each' : ''}`) : '';
    const chips = [
      ctx.coming && (s.slot === 'dinner' || s.slot === 'lunch') ? `<span class="it-chip soon" title="Coming soon">${ic('calendar-check')} Hold a table <b>Soon</b></span>` : '',
      each ? `<span class="it-chip">${esc(each)}</span>` : '',
      s.rating ? `<span class="it-chip">★ ${esc(s.rating)}</span>` : '',
      s.address ? `<span class="it-chip">${esc(s.address)}</span>` : '',
    ].filter(Boolean).join('');
    const dir = `https://www.google.com/maps/search/?api=1&query=${encodeURIComponent(s.lat != null ? `${s.lat},${s.lng}` : `${s.name} ${s.address || ''}`)}`;
    const site = safeUrl(s.url);
    const links = [
      !ctx.readOnly && ctx.on.swap ? `<button type="button" data-swap="${i}">${ic('arrows-clockwise')} Swap</button>` : '',
      `<a href="${esc(dir)}" target="_blank" rel="noopener">${ic('navigation-arrow')} Directions</a>`,
      site ? `<a href="${esc(site)}" target="_blank" rel="noopener">${ic(s.slot === 'event' ? 'ticket' : 'globe-simple')} ${s.slot === 'event' ? 'Tickets' : s.source === 'yelp' ? 'Yelp' : 'Website'}</a>` : '',
      !ctx.readOnly && ctx.on.remove && ctx.count > 1 ? `<button type="button" data-rm="${i}">${ic('x')} Remove</button>` : '',
    ].filter(Boolean).join('');
    const sub = [slotName || cat, s.slot === 'event' && t != null ? `starts ${short(t)}` : s.price || ''].filter(Boolean).join(' · ');
    return `<article class="it-stop" data-i="${i}">
      <div class="it-time"><b class="num">${t != null ? short(t) : pad(i + 1)}</b><small>${esc(note)}</small></div>
      <div class="it-card">
        ${photo(s, 'it-ph')}
        <button type="button" class="it-row" aria-expanded="false" aria-controls="itm${i}">
          <span class="it-n">${esc(s.name)}</span><span class="it-sub">${esc(sub)}</span></button>
        <span class="it-price num">${s.est_cost != null ? (s.est_cost === 0 ? 'Free' : money(s.est_cost)) : ''}</span>
        <div class="it-info" id="itm${i}">
          ${kicker ? `<span class="it-cat">${esc(kicker)}</span>` : ''}
          <h3>${esc(s.name)}</h3>
          ${s.why ? `<p>${esc(s.why)}</p>` : ''}
          ${chips ? `<div class="it-chips">${chips}</div>` : ''}
          <div class="it-links">${links}</div>
        </div>
      </div>
    </article>`;
  }

  function walkHtml(a, b, transport){
    const km = legKm(a, b);
    if (km == null) return '<div class="it-walk"><span></span><div><span class="it-dash"></span></div></div>';
    const tb = parseClock(b.time);
    const walking = transport === 'walk' || km < 1.6;
    const mins = walking ? walkMin(km) : null;
    const leave = walking && tb != null ? ` <span class="it-dot">·</span> Leave at ${short(tb - mins)}` : '';
    const what = walking ? `${ic('person-simple-walk')} ${mins} min walk · ${(Math.round(km * 10) / 10)} km`
      : `${ic(transport === 'car' ? 'car' : 'train-simple')} ${(Math.round(km * 10) / 10)} km`;
    return `<div class="it-walk"><span></span><div><span class="it-dash"></span>${what}${leave}</div></div>`;
  }

  function costBox(plans, st){
    const rows = plans.flatMap(p => p.stops || []).filter(s => s.est_cost != null)
      .map(s => `<div><span>${esc(s.name)}<span class="it-dim"> · ${esc(slotOf(s)[1].toLowerCase() || 'stop')}</span></span><span class="num">${s.est_cost === 0 ? 'Free' : money(s.est_cost)}</span></div>`).join('');
    const budget = plans.length === 1 && plans[0].budget ? +plans[0].budget : null;
    const note = [st.pp > 1 ? `${money(st.total)} for ${st.pp}` : '',
      budget ? (st.total <= budget ? `under your ${money(budget)} budget` : `about ${money(st.total - budget)} over your ${money(budget)} budget`) : ''].filter(Boolean).join(', ');
    return `<div class="it-box"><span class="it-label">Cost per person</span>
      ${rows ? `<div class="it-cost">${rows}</div>` : ''}
      <div class="it-total"><span>Total each</span><b class="num">${money(st.each)}</b></div>
      <p class="it-fine">${note ? esc(cap(note)) + '. ' : ''}Estimates are rough, from each place's price level and ticket prices.</p></div>`;
  }

  function directionsUrl(plans){
    const pts = plans.flatMap(p => p.stops || []).filter(s => s.lat != null);
    if (!pts.length) return null;
    const last = pts[pts.length - 1], mid = pts.slice(0, -1).slice(0, 8);
    return `https://www.google.com/maps/dir/?api=1&travelmode=walking&destination=${last.lat},${last.lng}`
      + (mid.length ? `&waypoints=${encodeURIComponent(mid.map(s => `${s.lat},${s.lng}`).join('|'))}` : '');
  }

  /* ---------------------------------------------------------------- render */
  let cur = null;   // { root, map, pins:[], stops:[] }

  function render(root, plan, o = {}){
    const on = o.on || {};
    const plans = o.days && o.days.length ? o.days : [plan];
    const st = stats(plans, o.transport);
    const allStops = plans.flatMap(p => p.stops || []);
    const covers = allStops.slice(0, 3);
    const dir = directionsUrl(plans);
    const ctx = { pp: st.pp, readOnly: !!o.readOnly, coming: !!o.coming, on };
    let gi = 0;
    const hours = plans.map((p, d) => {
      const n = (p.stops || []).length;
      const body = (p.stops || []).map((s, i) => {
        const h = stopHtml(s, gi, { ...ctx, count: plans.length > 1 ? Infinity : n }) + (i < n - 1 ? walkHtml(s, p.stops[i + 1], o.transport) : '');
        gi++; return h;
      }).join('');
      return (plans.length > 1 ? `<h2 class="it-day">Day ${esc(p.day || d + 1)}${p.estimated_cost ? ` <span class="it-dim">· about ${money(p.estimated_cost / st.pp)} each</span>` : ''}</h2>` : '') + body;
    }).join('');
    const endNote = st.end != null && plans.length === 1
      ? `<div class="it-end"><span></span><div>${ic('moon-stars')} Done by about ${short(st.end)}.${!o.readOnly && on.lateBite ? `<button type="button" data-late>${ic('plus')} Add a late bite</button>` : ''}</div></div>` : '';
    const statCells = [
      st.span ? [st.span, plans.length > 1 ? 'Day one' : 'Tonight', 'span'] : null,
      st.dur ? [st.dur, nWord(st.n), 'dur'] : [String(st.n), 'Stops', 'dur'],
      st.km != null ? [`${st.km} km`, st.onFoot ? 'All on foot' : 'Stop to stop', 'km'] : null,
      [`${money(st.each)}<small> ${st.pp > 1 ? 'each' : ''}</small>`, st.pp > 1 ? `${money(st.total)} for ${st.pp}` : 'In total', 'cost'],
    ].filter(Boolean);
    const saveBtn = on.save ? `<button type="button" class="it-btn${o.saved ? ' done' : ''}" data-save>${ic(o.saved ? 'check' : 'bookmark-simple')} ${o.saved ? 'Saved' : 'Save'}</button>` : '';
    const shareBtn = on.share ? `<button type="button" class="it-btn acc" data-share>${ic('export')} Share the night</button>` : '';

    root.innerHTML = `<div class="itin${o.readOnly ? ' ro' : ''}">
      <header class="it-bar">
        <a class="it-wm" href="/" aria-label="Rova home"><svg width="85" height="20" aria-hidden="true"><use href="/brand/rova.svg#wordmark"/></svg></a>
        ${o.barLinks || ''}
        <div class="it-bar-r">
          ${o.coming ? `<span class="it-together" title="Coming soon"><span class="it-avs"><i>+</i><i></i></span>Plan together <b>Soon</b></span>` : ''}
          ${o.barRight || ''}
          ${on.settings ? `<button type="button" class="it-ib" data-settings aria-label="Plan settings">${ic('sliders-horizontal')}</button>` : ''}
          ${on.close ? `<button type="button" class="it-ib" data-close aria-label="Back to the map">${ic('x')}</button>` : ''}
        </div>
      </header>

      <div class="it-cover">
        ${photo(covers[0], 'it-cover-ph', true)}
        <div class="it-cover-top">
          ${on.close ? `<button type="button" class="it-round" data-close aria-label="Back to the map">${ic('caret-left')}</button>` : `<a class="it-round" href="/" aria-label="Rova home">${ic('caret-left')}</a>`}
          <span>${allStops.some(s => s.lat != null) ? `<button type="button" class="it-round" data-tomap aria-label="See the route">${ic('map-trifold')}</button>` : ''}
            ${on.save ? `<button type="button" class="it-round" data-save aria-label="${o.saved ? 'Saved' : 'Save'}">${ic(o.saved ? 'check' : 'bookmark-simple')}</button>` : ''}</span>
        </div>
        <div class="it-cover-t"><span class="it-kicker">${esc(o.kickerShort || o.kicker || '')}</span><h1 class="it-h1 sm">${titleHtml(o.title)}</h1></div>
      </div>

      <section class="it-hero it-wrap">
        ${on.close ? `<button type="button" class="it-back" data-close>${ic('arrow-left')} Back to the map</button>` : ''}
        <div class="it-titlerow">
          <div class="it-tt"><span class="it-kicker">${esc(o.kicker || '')}</span><h1 class="it-h1">${titleHtml(o.title)}</h1>
            ${o.ctxHtml ? `<p class="it-ctx" data-ctxline>${o.ctxHtml}</p>` : ''}</div>
          <div class="it-acts">${saveBtn}${shareBtn}</div>
        </div>
        <div class="it-stats" style="--n:${statCells.length}">${statCells.map(([v, l, k]) => `<div class="s-${k}"><b class="num">${v}</b><span>${esc(l)}</span></div>`).join('')}</div>
        ${covers.length ? `<div class="it-collage n${covers.length}">${covers.map((s, i) => `<div class="it-tile">${photo(s, 'it-tile-ph', true)}
          <span class="it-cap"><i>${i + 1}</i>${parseClock(s.time) != null ? short(parseClock(s.time)) + ' · ' : ''}${esc(s.name)}</span></div>`).join('')}</div>` : ''}
      </section>

      <div class="it-body it-wrap">
        <div class="it-main">
          ${o.ctxHtml ? `<p class="it-ctx it-ctx-ph" data-ctxline>${o.ctxHtml}</p>` : ''}
          ${o.intro ? `<p class="it-intro">${esc(o.intro)}</p>` : ''}
          <span class="it-label">Hour by hour</span>
          <div class="it-stops">${hours}</div>
          ${endNote}
          ${(o.fits || []).length ? `<span class="it-label it-gap">Why this fits</span><ul class="it-fits">${o.fits.map(f =>
            `<li class="${f.ok ? '' : 'warn'}">${ic(f.ok ? 'check' : 'warning-circle')}<span>${esc(f.t)}</span></li>`).join('')}</ul>` : ''}
          ${plans.length === 1 && plan.tip ? `<p class="it-tip">${ic('lightbulb')} ${esc(plan.tip)}</p>` : ''}
          ${(o.also || []).length ? `<span class="it-label it-gap">Also on nearby</span><div class="it-also">${o.also.map((e, i) => `<div class="it-also-row">
            ${photo({ image:e.img, slot:'event' }, 'it-also-ph')}<div><b>${esc(e.name)}</b><small>${esc(e.sub || '')}</small></div>
            <button type="button" class="it-btn sm" data-also="${i}">${ic('plus')} Add</button></div>`).join('')}</div>` : ''}
        </div>
        <aside class="it-side">
          ${allStops.some(s => s.lat != null) ? `<div class="it-mapbox" id="itMapBox"><div class="it-map" id="itMap" role="region" aria-label="Route map"></div>
            <span class="it-maplabel" id="itMapLabel">Route${st.km != null ? ` · ${st.km} km${st.onFoot ? ' on foot' : ''}` : ''}</span></div>` : ''}
          ${costBox(plans, st)}
          ${!o.readOnly && (o.tweaks || on.ask) ? `<div class="it-box"><span class="it-label">Make it yours</span>
            <div class="it-tweaks">${(o.tweaks || []).map((t, i) => `<button type="button" class="it-tw" data-tw="${i}">${esc(t)}</button>`).join('')}
              ${on.again ? `<button type="button" class="it-tw" data-again>${ic('arrows-clockwise')} Try another</button>` : ''}
              ${on.surprise ? `<button type="button" class="it-tw" data-surprise>${ic('shuffle')} Surprise me</button>` : ''}</div>
            ${on.ask ? `<form class="it-ask" data-ask><input name="q" maxlength="300" placeholder="Or tell Rova what to change" aria-label="Tell Rova what to change" autocomplete="off">
              <button type="submit" aria-label="Send">${ic('arrow-up')}</button></form>` : ''}</div>` : ''}
          ${o.coming ? `<div class="it-friends"><div class="it-fr-top"><span class="it-avs lg"><i>+</i><i></i></span>
              <div><b>Going with friends?</b><span>Soon they'll vote on stops and see the route with you.</span></div></div>
            <div class="it-fr-acts"><button type="button" class="it-fr-send" disabled>${ic('paper-plane-tilt')} Send to friends <b>Coming soon</b></button>
              ${on.share ? `<button type="button" class="it-fr-link" data-share aria-label="Share a link instead">${ic('link')}</button>` : ''}</div></div>` : ''}
          ${o.sideExtra || ''}
        </aside>
      </div>

      <div class="it-dock">
        ${on.share ? `<button type="button" class="it-btn" data-share>${ic('export')} Share</button>` : ''}
        ${dir ? `<a class="it-btn acc" href="${esc(dir)}" target="_blank" rel="noopener">${ic('navigation-arrow')} Let's go</a>` : ''}
      </div>
    </div>`;

    // wiring
    const all = sel => root.querySelectorAll(sel);
    const wire = (sel, fn) => all(sel).forEach(b => b.addEventListener('click', e => fn(e, b)));
    if (on.close) wire('[data-close]', () => on.close());
    if (on.save) wire('[data-save]', (e, b) => on.save(b));
    if (on.share) wire('[data-share]', (e, b) => on.share(b));
    if (on.settings) wire('[data-settings]', () => on.settings());
    if (on.swap) wire('[data-swap]', (e, b) => on.swap(+b.dataset.swap));
    if (on.remove) wire('[data-rm]', (e, b) => on.remove(+b.dataset.rm));
    if (on.tweak) wire('[data-tw]', (e, b) => on.tweak(+b.dataset.tw));
    if (on.again) wire('[data-again]', () => on.again());
    if (on.surprise) wire('[data-surprise]', () => on.surprise());
    if (on.also) wire('[data-also]', (e, b) => on.also(+b.dataset.also));
    if (on.lateBite) wire('[data-late]', () => on.lateBite());
    wire('[data-tomap]', () => { const m = root.querySelector('#itMapBox'); if (m) m.scrollIntoView({ behavior:'smooth', block:'center' }); });
    const ask = root.querySelector('[data-ask]');
    if (ask && on.ask) ask.addEventListener('submit', e => { e.preventDefault(); const v = ask.q.value.trim(); if (v){ on.ask(v); ask.reset(); } });
    // phone: a stop's details open under its row
    all('.it-row').forEach(b => b.addEventListener('click', () => {
      const art = b.closest('.it-stop'), open = !art.classList.contains('open');
      art.classList.toggle('open', open); b.setAttribute('aria-expanded', open);
    }));

    if (cur && cur.map){ cur.map.remove(); }
    cur = { root, map:null, pins:[], stops:allStops, label:root.querySelector('#itMapLabel'), base:root.querySelector('#itMapLabel') && root.querySelector('#itMapLabel').textContent };
    // hover a stop → light it on the route
    all('.it-stop').forEach(a => {
      const i = +a.dataset.i;
      a.addEventListener('mouseenter', () => light(i));
      a.addEventListener('mouseleave', () => light(-1));
      a.addEventListener('focusin', () => light(i));
    });
    initMap();
    return root;
  }

  function initMap(){
    const el = cur.root.querySelector('#itMap');
    if (!el || !window.L) return;
    const pts = cur.stops.map((s, i) => s.lat != null ? { i, ll:[s.lat, s.lng], s } : null).filter(Boolean);
    if (!pts.length) return;
    const map = L.map(el, { zoomControl:false, attributionControl:true, scrollWheelZoom:false, zoomSnap:0.25 });
    map.attributionControl.setPrefix(false);
    L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', { maxZoom:19,
      attribution:'© <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener">OpenStreetMap</a>' }).addTo(map);
    if (pts.length > 1) L.polyline(pts.map(p => p.ll), { color:'#D4B47A', weight:2.5, opacity:1, lineJoin:'round', lineCap:'round' }).addTo(map);
    cur.pins = [];
    pts.forEach(p => {
      const m = L.marker(p.ll, { title:p.s.name, keyboard:false,
        icon:L.divIcon({ className:'', html:`<span class="it-pin">${p.i + 1}</span>`, iconSize:[30,30], iconAnchor:[15,15] }) }).addTo(map);
      m.on('click', () => focus(p.i));
      cur.pins[p.i] = m;
    });
    if (pts.length === 1) map.setView(pts[0].ll, 15);
    else map.fitBounds(L.latLngBounds(pts.map(p => p.ll)), { padding:[48, 48], maxZoom:16 });
    cur.map = map;
    setTimeout(() => map.invalidateSize(), 60);
  }

  function light(i){
    if (!cur) return;
    cur.root.querySelectorAll('.it-stop').forEach(a => a.classList.toggle('on', +a.dataset.i === i));
    cur.pins.forEach((m, j) => { const el = m && m.getElement(); if (el && el.firstChild) el.firstChild.classList.toggle('on', j === i); });
    if (cur.label){
      const s = cur.stops[i];
      cur.label.textContent = s ? [s.name, s.address].filter(Boolean).join(' · ') : cur.base;
    }
  }

  function focus(i){
    if (!cur) return;
    const a = cur.root.querySelector(`.it-stop[data-i="${i}"]`);
    if (a){
      a.scrollIntoView({ behavior:'smooth', block:'center' });
      if (getComputedStyle(a.querySelector('.it-row')).display !== 'none'){
        a.classList.add('open'); a.querySelector('.it-row').setAttribute('aria-expanded', 'true');
      }
    }
    light(i);
  }

  /* Loading / error, in the same frame so the page doesn't jump. */
  function status(root, o = {}){
    const on = o.on || {};
    if (cur && cur.map){ cur.map.remove(); cur = null; }
    root.innerHTML = `<div class="itin">
      <header class="it-bar"><a class="it-wm" href="/" aria-label="Rova home"><svg width="85" height="20" aria-hidden="true"><use href="/brand/rova.svg#wordmark"/></svg></a>
        <div class="it-bar-r">${on.close ? `<button type="button" class="it-ib" data-close aria-label="Back to the map">${ic('x')}</button>` : ''}</div></header>
      <section class="it-hero it-wrap it-status">
        ${on.close ? `<button type="button" class="it-back" data-close>${ic('arrow-left')} Back to the map</button>` : ''}
        <span class="it-kicker">${esc(o.kicker || '')}</span>
        <h1 class="it-h1">${titleHtml(o.title || 'Your night')}</h1>
        ${o.ctxHtml ? `<p class="it-ctx" data-ctxline>${o.ctxHtml}</p>` : ''}
        ${o.error ? `<div class="it-err">${ic('warning-circle')}<div><b>${esc(o.error.title)}</b><p>${esc(o.error.text)}</p>
            <div class="it-err-acts">${on.retry ? `<button type="button" class="it-btn acc" data-retry>${ic('arrow-clockwise')} Try again</button>` : ''}
              ${on.settings ? `<button type="button" class="it-btn" data-settings>${ic('sliders-horizontal')} Adjust settings</button>` : ''}</div></div></div>`
          : `<p class="it-wait" role="status" id="itWait"><span class="it-dots" aria-hidden="true"><i></i><i></i><i></i></span> ${esc(o.line || '')}</p>
            <div class="it-stats it-skel-stats" aria-hidden="true">${[0,1,2,3].map(() => '<div><i class="it-skel" style="width:70%;height:30px"></i><i class="it-skel" style="width:40%;height:9px"></i></div>').join('')}</div>
            <div class="it-collage n3 it-skel-col" aria-hidden="true"><i class="it-skel"></i><i class="it-skel"></i><i class="it-skel"></i></div>`}
      </section></div>`;
    root.querySelectorAll('[data-close]').forEach(b => b.onclick = () => on.close());
    const r = root.querySelector('[data-retry]'); if (r) r.onclick = () => on.retry();
    const s = root.querySelector('[data-settings]'); if (s) s.onclick = () => on.settings();
    return root.querySelector('#itWait');
  }

  return { render, status, focus, splitTitle, stats, directionsUrl };
})();
