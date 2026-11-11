/* homeSec console: live view, motion meter, activity, detection switch and a slider with past events
   (people = alerts, birds = visits). Plain JavaScript, no libraries. Everything shown comes from
   /api/status, /api/events, /api/visits and /stream.mjpg. */
(() => {
  'use strict';

  const camel = (id) => id.replace(/-(\w)/g, (_, c) => c.toUpperCase());
  const el = Object.fromEntries([
    'subtitle', 'detection', 'detection-label', 'live', 'live-label', 'stage', 'stream', 'overlay',
    'overlay-text', 'stamp', 'motion-badge', 'rec-badge', 'rec-text', 'fullscreen', 'stream-info', 'viewers',
    'meter', 'meter-fill', 'score', 'threshold', 'spark', 'events-today', 'visits-today', 'birds-today-row',
    'visit-what', 'last-motion', 'uptime', 'alerts', 'lightbox', 'lightbox-img', 'lightbox-caption',
    'lightbox-download', 'lightbox-clip', 'lightbox-delete', 'lightbox-video', 'disk', 'disk-free', 'disk-jpg',
    'disk-gif', 'disk-mp4', 'disk-rest', 'disk-other', 'disk-detail', 'disk-kind-jpg', 'disk-kind-gif',
    'disk-kind-mp4', 'tab-people', 'tab-birds', 'tab-all', 'tab-birds-label', 'count-people', 'count-birds',
    'count-all', 'older', 'newer', 'latest', 'strip',
  ].map((id) => [camel(id), document.getElementById(id)]));

  const POLL_MS = 600;       // status refresh while the page is visible
  const RETRY_MS = 3000;     // wait before reconnecting a broken stream
  const HISTORY = 80;        // points in the motion graph
  const METER_MARK = 0.6;    // the alert threshold sits at 60% of the meter (see style.css)

  let status = null;
  let offline = false;
  let streamError = false;
  let skew = 0;              // server clock minus browser clock, in seconds
  let eventsKey = null;
  let visitsKey = null;
  const history = [];

  // ---------- helpers
  const pad = (n) => String(n).padStart(2, '0');
  const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  const serverNow = () => new Date((Date.now() / 1000 + skew) * 1000);
  const dateText = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
  const timeText = (d) => `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;

  async function getJSON(url) {
    const response = await fetch(url, { cache: 'no-store' });
    if (!response.ok) throw new Error(`${url}: ${response.status}`);
    return response.json();
  }

  function ago(ts) {
    const s = Math.max(0, Math.round(Date.now() / 1000 + skew - ts));
    if (s < 60) return `${s} s ago`;
    if (s < 3600) return `${Math.floor(s / 60)} min ago`;
    if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
    return `${Math.floor(s / 86400)} d ago`;
  }

  function duration(seconds) {
    if (seconds < 90) return `${seconds} s`;
    if (seconds < 5400) return `${Math.round(seconds / 60)} min`;
    if (seconds < 172800) return `${Math.floor(seconds / 3600)} h ${Math.round((seconds % 3600) / 60)} min`;
    return `${Math.floor(seconds / 86400)} d ${Math.floor((seconds % 86400) / 3600)} h`;
  }

  // ---------- live stream
  let view = 'live';         // the open tab: live, files or stats
  function startStream() {
    if (document.hidden || view !== 'live' || el.stream.getAttribute('src')) return;
    streamError = false;
    el.stream.src = `/stream.mjpg?t=${Date.now()}`;
  }

  function stopStream() {
    el.stream.removeAttribute('src');
  }

  function updateLive() {
    let state = 'connecting';
    let text = 'Connecting';
    let message = '';
    if (offline) {
      state = 'offline'; text = 'Offline'; message = 'No contact with the Pi. Trying again…';
    } else if (streamError) {
      state = 'retry'; text = 'Reconnecting'; message = 'The camera stream was interrupted. Trying again…';
    } else if (el.stream.naturalWidth > 0) {
      state = 'live'; text = 'Live';
    }
    el.live.dataset.state = state;
    el.liveLabel.textContent = text;
    el.overlay.hidden = !message;
    el.overlayText.textContent = message;
  }

  el.stream.addEventListener('load', () => { streamError = false; updateLive(); });
  el.stream.addEventListener('error', () => {
    streamError = true;
    stopStream();
    updateLive();
    setTimeout(startStream, RETRY_MS);
  });

  // The Pi only encodes pictures for the live view while someone is watching, so stop when the tab is hidden.
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) stopStream(); else startStream();
  });

  el.fullscreen.addEventListener('click', () => {
    if (document.fullscreenElement) document.exitFullscreen();
    else if (el.stage.requestFullscreen) el.stage.requestFullscreen();
  });

  // ---------- detection switch
  el.detection.addEventListener('click', async () => {
    if (!status) return;
    const next = status.detection === 'on' ? 'off' : 'on';
    el.detection.disabled = true;
    try {
      const response = await fetch('/api/detection', {
        method: 'POST',
        headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
        body: `state=${next}`,
      });
      if (!response.ok) throw new Error(String(response.status));
      render(await response.json());
    } catch (err) {
      el.detectionLabel.textContent = 'Could not switch';
    } finally {
      el.detection.disabled = false;
    }
  });

  // ---------- motion graph
  function drawSpark() {
    const canvas = el.spark;
    const ratio = window.devicePixelRatio || 1;
    const w = canvas.clientWidth;
    const h = canvas.clientHeight;
    if (!w || !h) return;
    if (canvas.width !== Math.round(w * ratio)) {
      canvas.width = Math.round(w * ratio);
      canvas.height = Math.round(h * ratio);
    }
    const g = canvas.getContext('2d');
    g.setTransform(ratio, 0, 0, ratio, 0, 0);
    g.clearRect(0, 0, w, h);

    const threshold = status ? status.threshold : 1;
    const top = Math.max(threshold * 1.6, ...history, 1);
    const y = (v) => h - 2 - (v / top) * (h - 6);

    g.setLineDash([4, 4]);
    g.globalAlpha = 0.65;
    g.strokeStyle = css('--danger');
    g.lineWidth = 1;
    g.beginPath();
    g.moveTo(0, y(threshold));
    g.lineTo(w, y(threshold));
    g.stroke();
    g.setLineDash([]);
    g.globalAlpha = 1;

    if (history.length < 2) return;
    const step = w / (HISTORY - 1);
    const x0 = w - (history.length - 1) * step;
    g.beginPath();
    history.forEach((v, i) => {
      const x = x0 + i * step;
      if (i) g.lineTo(x, y(v)); else g.moveTo(x, y(v));
    });
    g.strokeStyle = css('--accent');
    g.lineWidth = 1.6;
    g.lineJoin = 'round';
    g.stroke();
    g.lineTo(w, h);
    g.lineTo(x0, h);
    g.closePath();
    g.globalAlpha = 0.14;
    g.fillStyle = css('--accent');
    g.fill();
    g.globalAlpha = 1;
  }

  // ---------- event log
  const ICONS = {
    download: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 4v11M7 10l5 5 5-5M5 20h14"/></svg>',
    trash: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 7h16M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7V4h6v3"/></svg>',
  };
  const ARM_MS = 4000;       // a delete button stays "armed" (click again to confirm) this long
  let viewing = null;        // the event open in the viewer

  const download = (url) => `${url}?download=1`;

  // Deleting needs two clicks: the first arms the button ("Delete?"), the second deletes.
  function armable(button, armedText, action) {
    let timer = null;
    const label = button.innerHTML;
    const disarm = () => {
      clearTimeout(timer);
      timer = null;
      button.classList.remove('armed');
      button.innerHTML = label;
    };
    button.addEventListener('click', async (e) => {
      e.stopPropagation();
      if (!timer) {
        button.classList.add('armed');
        button.textContent = armedText;
        timer = setTimeout(disarm, ARM_MS);
        return;
      }
      disarm();
      button.disabled = true;
      try {
        await action();
      } catch (err) {
        button.textContent = 'Failed';
        setTimeout(() => { button.innerHTML = label; }, 2000);
      } finally {
        button.disabled = false;
      }
    });
    return disarm;
  }

  async function post(url, name) {
    const response = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
      body: `name=${encodeURIComponent(name)}`,
    });
    if (!response.ok && response.status !== 404) throw new Error(String(response.status));
    return response.ok ? response.json() : null;
  }

  async function deleteEvent(event) {
    const answer = await post('/api/events/delete', event.name);
    if (viewing && viewing.name === event.name) el.lightbox.close();
    eventsKey = null;        // counts in the status change too
    film.people = answer ? answer.events : (await getJSON('/api/events')).events;
    renderFilm();
  }

  async function deleteVisit(visit) {
    const answer = await post('/api/visits/delete', visit.name);
    if (viewing && viewing.name === visit.name) el.lightbox.close();
    visitsKey = null;
    film.birds = answer ? answer.visits : (await getJSON('/api/visits')).visits;
    renderFilm();
  }

  const disarmViewer = armable(el.lightboxDelete, 'Delete? Click again',
    () => (viewing && viewing.kind === 'visit' ? deleteVisit(viewing) : deleteEvent(viewing)));

  function showMedia(video) {
    el.lightboxImg.hidden = video;
    el.lightboxVideo.hidden = !video;
  }

  const sure = (x) => (x == null ? '' : `${Math.round(x * 100)}% sure`);
  const seconds = (x) => (x ? `${x} s` : '');

  function openPicture(event) {
    viewing = { ...event, kind: 'event' };
    const moving = event.video || event.clip;  // the alert video, or the older GIF clip
    showMedia(Boolean(event.video));
    if (event.video) {
      el.lightboxVideo.src = event.video;
      el.lightboxVideo.poster = event.url;
    } else {
      el.lightboxImg.src = event.clip || event.url;
    }
    el.lightboxCaption.textContent = [`${event.date}  ${event.time}`, event.label, sure(event.confidence),
      event.video ? `video ${seconds(event.seconds)}`.trim() : event.clip ? 'clip' : ''].filter(Boolean).join('  · ');
    el.lightboxDownload.textContent = 'Picture';
    el.lightboxDownload.href = download(event.url);
    el.lightboxClip.hidden = !moving;
    if (moving) {
      el.lightboxClip.textContent = event.video ? 'Video' : 'Clip';
      el.lightboxClip.href = download(moving);
    }
    disarmViewer();
    el.lightbox.showModal();
    if (event.video) el.lightboxVideo.play().catch(() => { /* the person can press play */ });
  }

  function openVisit(visit) {
    viewing = { ...visit, kind: 'visit' };
    showMedia(true);
    el.lightboxVideo.src = visit.url;
    if (visit.thumb) el.lightboxVideo.poster = visit.thumb;
    el.lightboxCaption.textContent = [`${visit.date}  ${visit.time}`, visit.label, seconds(visit.seconds),
      visit.mb != null ? `${visit.mb} MB` : ''].filter(Boolean).join('  · ');
    el.lightboxDownload.textContent = 'Video';
    el.lightboxDownload.href = download(visit.url);
    el.lightboxClip.hidden = true;
    disarmViewer();
    el.lightbox.showModal();
    el.lightboxVideo.play().catch(() => { /* the person can press play */ });
  }

  el.lightbox.addEventListener('click', (e) => { if (e.target === el.lightbox) el.lightbox.close(); });
  el.lightbox.addEventListener('close', () => {
    el.lightboxImg.removeAttribute('src');
    el.lightboxVideo.pause();
    el.lightboxVideo.removeAttribute('src');
    el.lightboxVideo.removeAttribute('poster');
    el.lightboxVideo.load();
    viewing = null;
  });

  // ---------- the timeline with past events: people (alerts) and birds (visits)
  // Oldest on the left, newest on the right; it opens at the newest end. The space between two events
  // grows with the time between them, and every new day gets a divider.
  const film = { tab: 'people', people: [], birds: [], visitsOn: false };
  try {
    const saved = localStorage.getItem('homesec.tab');
    if (['people', 'birds', 'all'].includes(saved)) film.tab = saved;
  } catch (err) { /* no storage: start with people */ }

  const atEnd = () => el.strip.scrollLeft >= el.strip.scrollWidth - el.strip.clientWidth - 8;
  function toNewest(smooth) {
    el.strip.scrollTo({ left: el.strip.scrollWidth, behavior: smooth ? 'smooth' : 'auto' });
  }

  function chooseTab(tab) {
    film.tab = tab;
    try { localStorage.setItem('homesec.tab', tab); } catch (err) { /* fine */ }
    renderFilm(true);
  }
  el.tabPeople.addEventListener('click', () => chooseTab('people'));
  el.tabBirds.addEventListener('click', () => chooseTab('birds'));
  el.tabAll.addEventListener('click', () => chooseTab('all'));

  // the arrows scroll by most of the visible width: left is older, right is newer
  const page = () => Math.max(200, el.strip.clientWidth * 0.85);
  el.older.addEventListener('click', () => el.strip.scrollBy({ left: -page() }));
  el.newer.addEventListener('click', () => el.strip.scrollBy({ left: page() }));
  el.latest.addEventListener('click', () => toNewest(true));
  function updateArrows() {
    const max = el.strip.scrollWidth - el.strip.clientWidth;
    el.older.disabled = el.strip.scrollLeft <= 2;
    el.newer.disabled = el.strip.scrollLeft >= max - 2;
    el.latest.disabled = el.newer.disabled;
  }
  el.strip.addEventListener('scroll', updateArrows, { passive: true });
  // a mouse wheel scrolls the slider sideways
  el.strip.addEventListener('wheel', (e) => {
    if (Math.abs(e.deltaY) > Math.abs(e.deltaX)) {
      el.strip.scrollBy({ left: e.deltaY, behavior: 'auto' });
      e.preventDefault();
    }
  }, { passive: false });

  function iconLink(href, title, icon) {
    const a = document.createElement('a');
    a.className = 'icon-btn small';
    a.href = href;
    a.setAttribute('download', '');
    a.title = title;
    a.setAttribute('aria-label', title);
    a.innerHTML = icon;
    a.addEventListener('click', (e) => e.stopPropagation());
    return a;
  }

  function card(item, kind, today) {
    const li = document.createElement('li');
    li.className = `card ${kind}`;
    const tick = document.createElement('span');
    tick.className = 'tick';
    tick.textContent = item.time.slice(0, 5);
    li.append(tick);
    const open = document.createElement('button');
    open.type = 'button';
    open.className = 'open';
    const when = `${item.date === today ? 'Today' : item.date} ${item.time}`;
    open.setAttribute('aria-label', kind === 'visit' ? `Play the visit from ${when}` : `Open the alert from ${when}`);
    const pic = document.createElement('div');
    pic.className = 'pic';
    const src = kind === 'visit' ? item.thumb : item.url;
    if (src) {
      const img = document.createElement('img');
      img.src = src;
      img.alt = '';
      img.loading = 'lazy';
      pic.append(img);
    }
    const hasVideo = kind === 'visit' || item.video;
    const badge = document.createElement('span');
    badge.className = 'badge';
    badge.textContent = hasVideo ? `▶ ${seconds(item.seconds) || 'video'}` : item.clip ? 'GIF' : 'Picture';
    pic.append(badge);
    const info = document.createElement('div');
    info.className = 'info';
    const line = document.createElement('div');
    line.className = 'line';
    const time = document.createElement('b');
    time.textContent = item.time;
    const what = document.createElement('span');
    const label = item.label || (kind === 'visit' ? 'visit' : 'motion');
    what.className = `what ${label}`;
    what.textContent = [label, item.confidence != null ? `${Math.round(item.confidence * 100)}%` : ''].filter(Boolean).join(' ');
    line.append(time, what);
    info.append(line);
    open.title = [`${item.date} ${item.time}`, label, sure(item.confidence)].filter(Boolean).join(' · ');
    open.append(pic, info);
    open.addEventListener('click', () => (kind === 'visit' ? openVisit(item) : openPicture(item)));

    const box = document.createElement('div');
    box.className = 'event-actions';
    const file = kind === 'visit' ? item.url : (item.video || item.url);
    box.append(iconLink(download(file), kind === 'visit' || item.video ? 'Download the video' : 'Download the picture',
      ICONS.download));
    const del = document.createElement('button');
    del.type = 'button';
    del.className = 'icon-btn small danger';
    del.title = 'Delete (click twice)';
    del.setAttribute('aria-label', `Delete the ${kind === 'visit' ? 'visit' : 'alert'} from ${when}`);
    del.innerHTML = ICONS.trash;
    armable(del, 'Delete?', () => (kind === 'visit' ? deleteVisit(item) : deleteEvent(item)));
    box.append(del);
    li.append(open, box);
    return li;
  }

  const DAY_NAMES = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
  const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

  function dayName(date, today) {
    if (date === today) return 'Today';
    const d = new Date(`${date}T12:00:00`);
    const y = new Date(`${today}T12:00:00`);
    y.setDate(y.getDate() - 1);
    if (dateText(d) === dateText(y)) return 'Yesterday';
    return `${DAY_NAMES[d.getDay()]} ${d.getDate()} ${MONTHS[d.getMonth()]}`;
  }

  function span(seconds) {
    if (seconds < 3600) return `${Math.round(seconds / 60)} min`;
    if (seconds < 86400) return `${Math.round(seconds / 3600)} h`;
    return `${Math.round(seconds / 86400)} d`;
  }

  // space between two events: 12 px for a few minutes, growing with the log of the time, at most 140 px
  const gapPx = (seconds) => Math.round(Math.min(140, 12 + Math.max(0, Math.log10(Math.max(seconds, 60) / 60)) * 38));

  function renderFilm(jump) {
    const keepEnd = jump || atEnd() || !el.strip.querySelector('.card');
    el.tabBirds.hidden = !film.visitsOn && !film.birds.length;
    el.tabAll.hidden = el.tabBirds.hidden;
    if (el.tabBirds.hidden && film.tab !== 'people') film.tab = 'people';
    for (const [tab, button] of [['people', el.tabPeople], ['birds', el.tabBirds], ['all', el.tabAll]]) {
      button.setAttribute('aria-selected', String(film.tab === tab));
    }
    el.countPeople.textContent = String(film.people.length);
    el.countBirds.textContent = String(film.birds.length);
    el.countAll.textContent = String(film.people.length + film.birds.length);

    const tagged = [
      ...(film.tab !== 'birds' ? film.people.map((item) => ({ item, kind: 'event' })) : []),
      ...(film.tab !== 'people' ? film.birds.map((item) => ({ item, kind: 'visit' })) : []),
    ].sort((a, b) => a.item.ts - b.item.ts);  // oldest first

    const today = dateText(serverNow());
    el.strip.replaceChildren();
    if (!tagged.length) {
      const li = document.createElement('li');
      li.className = 'empty';
      li.textContent = film.tab === 'birds' ? 'No visits yet…' : film.tab === 'all' ? 'No events yet…' : 'No alerts yet…';
      el.strip.append(li);
    }
    let previous = null;
    for (const { item, kind } of tagged) {
      if (!previous || previous.date !== item.date) {
        const day = document.createElement('li');
        day.className = 'day';
        day.setAttribute('aria-label', dayName(item.date, today));
        const label = document.createElement('span');
        label.textContent = dayName(item.date, today);
        day.append(label);
        el.strip.append(day);
      } else {
        const between = item.ts - previous.ts;
        const gap = document.createElement('li');
        gap.className = 'gap';
        gap.setAttribute('aria-hidden', 'true');
        gap.style.flexBasis = `${gapPx(between)}px`;
        if (between >= 3600) gap.dataset.span = span(between);
        el.strip.append(gap);
      }
      el.strip.append(card(item, kind, today));
      previous = item;
    }
    const now = document.createElement('li');  // the end of the line: now
    now.className = 'now';
    now.setAttribute('aria-hidden', 'true');
    now.textContent = 'now';
    el.strip.append(now);
    requestAnimationFrame(() => {
      if (keepEnd) toNewest(false);
      updateArrows();
    });
  }

  async function loadEvents() {
    try {
      film.people = (await getJSON('/api/events')).events;
      renderFilm();
    } catch (err) {
      eventsKey = null;      // the next status poll will try again
    }
  }

  async function loadVisits() {
    try {
      film.birds = (await getJSON('/api/visits')).visits;
      renderFilm();
    } catch (err) {
      visitsKey = null;
    }
  }

  function updateVisits(v) {
    film.visitsOn = Boolean(v && v.enabled);
    const name = v && v.objects.length ? v.objects.join(', ') : 'bird';
    el.tabBirdsLabel.textContent = name === 'bird' ? 'Birds' : `Visits (${name})`;
    el.visitWhat.textContent = name === 'bird' ? 'Bird' : 'Other';
    el.birdsTodayRow.hidden = !film.visitsOn && !(v && v.last);
    el.visitsToday.textContent = String(v ? v.today : 0);
    el.recBadge.hidden = !(v && v.recording);
    if (v && v.recording) el.recText.textContent = `REC · ${v.recording}`;
    const key = v ? `${v.today}|${v.last}|${v.recording}` : '';
    if (key !== visitsKey) {
      visitsKey = key;
      if (v && (v.enabled || v.last)) loadVisits(); else renderFilm();
    }
  }

  // ---------- disk
  const size = (bytes) => (!bytes ? '0 MB' : bytes >= 1e9 ? `${(bytes / 1e9).toFixed(1)} GB`
    : bytes >= 1e6 ? `${(bytes / 1e6).toFixed(bytes >= 1e8 ? 0 : 1)} MB` : `${Math.max(1, Math.round(bytes / 1e3))} kB`);

  function renderDisk(d) {
    el.disk.hidden = !d;
    if (!d) return;
    const pct = (n) => (d.total ? (100 * n) / d.total : 0);
    el.diskFree.textContent = `${size(d.free)} (${Math.round(pct(d.free))}%)`;
    const files = d.files || {};
    let known = 0;
    for (const kind of ['jpg', 'gif', 'mp4']) {
      const f = files[kind] || { count: 0, bytes: 0, folders: {} };
      known += f.bytes;
      el[`disk${kind[0].toUpperCase()}${kind.slice(1)}`].style.width = `${pct(f.bytes)}%`;
      const item = el[`diskKind${kind[0].toUpperCase()}${kind.slice(1)}`];
      item.querySelector('b').textContent = `${f.count} · ${size(f.bytes)}`;
      item.title = Object.entries(f.folders).map(([where, n]) => `${where}: ${n}`).join(', ') || 'none';
    }
    el.diskRest.style.width = `${Math.max(0, pct(d.homesec - known))}%`;  // homeSec's other files
    el.diskOther.style.width = `${Math.max(0, pct(d.total - d.free - d.homesec))}%`;
    el.diskDetail.textContent = `homeSec ${size(d.homesec)} · system ${size(Math.max(0, d.total - d.free - d.homesec))} · disk ${size(d.total)}`;
    el.disk.dataset.level = d.level;
    el.disk.title = d.level === 'ok' ? '' : 'Disk space is running out: delete old pictures and videos, or lower KEEP_DAYS / VISIT_KEEP_DAYS';
  }

  // ---------- status
  function render(s) {
    status = s;
    offline = false;
    skew = s.now - Date.now() / 1000;
    const paused = s.detection === 'paused';

    el.subtitle.textContent = [s.camera, s.resolution, s.host].filter(Boolean).join(' · ');
    el.detection.dataset.state = s.detection;
    el.detection.setAttribute('aria-pressed', String(!paused));
    el.detection.title = paused ? 'Click to resume motion detection' : 'Click to pause motion detection';
    el.detectionLabel.textContent = paused ? 'Detection paused' : 'Detection on';
    document.title = `${paused ? '⏸ ' : ''}homeSec Console`;

    const full = s.threshold > 0 ? s.threshold / METER_MARK : 1;
    const pct = Math.max(0, Math.min(100, (s.score / full) * 100));
    el.meterFill.style.clipPath = `inset(0 ${100 - pct}% 0 0)`;
    el.meter.style.opacity = paused ? '0.35' : '1';
    el.meter.setAttribute('aria-valuenow', String(Math.round(pct)));
    el.score.textContent = `${s.score.toFixed(1)}%`;
    el.threshold.textContent = `${s.threshold}%`;

    const hot = !paused && s.score > s.threshold;
    el.stage.classList.toggle('alert', hot);
    el.motionBadge.hidden = !hot;

    history.push(s.score);
    if (history.length > HISTORY) history.shift();
    drawSpark();

    el.eventsToday.textContent = String(s.events_today);
    el.lastMotion.textContent = s.last_motion ? ago(s.last_motion.ts) : '—';
    el.lastMotion.title = s.last_motion ? `${s.last_motion.date} ${s.last_motion.time}` : '';
    el.uptime.textContent = duration(s.uptime);
    renderDisk(s.disk);
    el.streamInfo.textContent = `${s.resolution} · MJPEG · ${s.fps} fps`;
    el.viewers.textContent = s.viewers === 1 ? '1 viewer' : `${s.viewers} viewers`;

    const targets = [];
    if (s.slack.enabled) targets.push(s.slack.channel ? `Slack ${s.slack.channel}` : 'Slack');
    targets.push('Mail');
    const only = s.check && s.check.enabled ? ` · only for ${s.check.objects.join(', ')}` : '';
    el.alerts.textContent = `Alerts → ${targets.join(' · ')}${only}`;

    updateVisits(s.visits);

    const key = `${s.events_today}|${s.last_motion ? s.last_motion.name : ''}`;  // new alert: reload the slider
    if (key !== eventsKey) {
      eventsKey = key;
      loadEvents();
      if (view === 'files') loadFiles();
      if (view === 'stats') loadStats();
    }
    updateLive();
  }

  async function poll() {
    try {
      render(await getJSON('/api/status'));
    } catch (err) {
      offline = true;
      updateLive();
    }
    setTimeout(poll, document.hidden ? 5000 : (offline ? 3000 : POLL_MS));
  }

  setInterval(() => {
    const now = serverNow();
    el.stamp.textContent = `${dateText(now)}  ${timeText(now)}`;
  }, 1000);
  window.addEventListener('resize', () => { drawSpark(); updateArrows(); });

  // ---------- Files: folders | the events in a folder | a preview of the chosen event
  const $ = (id) => document.getElementById(id);
  const files = { folder: 'all', events: [], folders: {}, pick: null, query: '', sort: 'new' };
  const FOLDERS = { all: ['All events', '#2dd4bf'], alerts: ['alerts', '#fb7185'], visits: ['visits', '#60a5fa'], rejected: ['rejected', '#8b9bb0'] };
  const FOLDER_ICON = (color) => `<svg viewBox="0 0 24 24" fill="none" stroke="${color}" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></svg>`;
  const fileOf = (e, kind) => e.files.find((f) => f.kind === kind);

  async function loadFiles() {
    try {
      const data = await getJSON('/api/files');
      files.events = data.events;
      files.folders = data.folders;
      $('files-path').textContent = data.path;
      if (files.pick && !files.events.some((e) => e.id === files.pick)) files.pick = null;
      renderFiles();
    } catch (err) { /* the next refresh tries again */ }
  }

  function renderFiles() {
    const today = dateText(serverNow());
    // folders
    const tree = $('tree');
    tree.replaceChildren();
    for (const [id, [name, color]] of Object.entries(FOLDERS)) {
      const list = id === 'all' ? Object.values(files.folders) : [files.folders[id] || { events: 0, bytes: 0 }];
      const li = document.createElement('li');
      const b = document.createElement('button');
      b.type = 'button';
      b.className = id === 'all' ? 'tree-item' : 'tree-item sub';
      b.setAttribute('aria-current', String(files.folder === id));
      b.innerHTML = FOLDER_ICON(color);
      const n = document.createElement('span');
      n.className = 'name';
      n.textContent = `${name} (${list.reduce((a, f) => a + f.events, 0)})`;
      const z = document.createElement('span');
      z.className = 'size';
      z.textContent = size(list.reduce((a, f) => a + f.bytes, 0));
      b.append(n, z);
      b.addEventListener('click', () => { files.folder = id; renderFiles(); });
      li.append(b);
      tree.append(li);
    }
    // events
    const q = files.query.trim().toLowerCase();
    let shown = files.events.filter((e) => files.folder === 'all' || e.folder === files.folder);
    if (q) {
      shown = shown.filter((e) => [e.date, e.time, e.label, e.folder, dayName(e.date, today)].join(' ').toLowerCase().includes(q));
    }
    shown = [...shown].sort(files.sort === 'old' ? (a, b) => a.ts - b.ts : files.sort === 'big' ? (a, b) => b.bytes - a.bytes : (a, b) => b.ts - a.ts);
    $('folder-name').textContent = FOLDERS[files.folder][0];
    $('folder-count').textContent = `· ${shown.length} event${shown.length === 1 ? '' : 's'} · ${size(shown.reduce((a, e) => a + e.bytes, 0))}`;
    const rows = $('rows');
    rows.replaceChildren();
    if (!shown.length) {
      const li = document.createElement('li');
      li.className = 'empty';
      li.textContent = q ? 'Nothing matches the search.' : 'No files here.';
      rows.append(li);
    }
    for (const e of shown) {
      const li = document.createElement('li');
      const b = document.createElement('button');
      b.type = 'button';
      b.className = 'row';
      b.setAttribute('aria-pressed', String(files.pick === e.id));
      const pic = document.createElement('span');
      pic.className = 'pic';
      const jpg = fileOf(e, 'jpg');
      if (jpg) {
        const img = document.createElement('img');
        img.src = jpg.url;
        img.alt = '';
        img.loading = 'lazy';
        pic.append(img);
      }
      const when = document.createElement('span');
      when.className = 'when';
      const t = document.createElement('b');
      t.textContent = `${dayName(e.date, today)} ${e.time.slice(0, 5)}`;
      const w = document.createElement('span');
      w.className = `what ${e.label}`;
      w.textContent = [e.label, sure(e.confidence)].filter(Boolean).join(' · ');
      when.append(t, w);
      const kinds = document.createElement('span');
      kinds.className = 'kinds';
      for (const f of e.files) {
        const k = document.createElement('span');
        k.className = `kind ${f.kind}`;
        k.textContent = f.kind;
        kinds.append(k);
      }
      const z = document.createElement('span');
      z.className = 'size';
      z.textContent = size(e.bytes);
      b.append(pic, when, kinds, z);
      b.addEventListener('click', () => { files.pick = e.id; renderFiles(); });
      li.append(b);
      rows.append(li);
    }
    renderPreview(today);
  }

  $('files-search').addEventListener('input', (e) => { files.query = e.target.value; renderFiles(); });
  $('files-sort').addEventListener('change', (e) => { files.sort = e.target.value; renderFiles(); });

  function renderPreview(today) {
    const e = files.events.find((x) => x.id === files.pick);
    const body = $('preview-body');
    document.querySelector('#preview .pick-hint').hidden = Boolean(e);
    body.hidden = !e;
    const video = $('preview-video');
    if (!e) {
      video.pause();
      video.removeAttribute('src');
      return;
    }
    if (body.dataset.id !== e.id) {     // a different event: new media
      body.dataset.id = e.id;
      const mp4 = fileOf(e, 'mp4');
      const still = fileOf(e, 'jpg') || fileOf(e, 'gif');
      video.pause();
      if (mp4) {
        video.src = mp4.url;
        if (still) video.poster = still.url; else video.removeAttribute('poster');
      } else {
        video.removeAttribute('src');
        video.removeAttribute('poster');
      }
      video.hidden = !mp4;
      $('preview-img').hidden = Boolean(mp4);
      const pic = fileOf(e, 'gif') || still;
      if (!mp4 && pic) $('preview-img').src = pic.url;
      $('preview-play').hidden = !mp4;
      disarmPreview();
    }
    $('preview-title').textContent = `${e.label} · ${dayName(e.date, today)} ${e.time}`;
    $('preview-detail').textContent = [sure(e.confidence), e.seconds ? `video ${e.seconds} s` : ''].filter(Boolean).join(' · ') || (e.folder === 'rejected' ? 'movement, but no person or bird seen' : 'picture only');
    $('preview-folder').textContent = e.folder;
    $('preview-keep').textContent = e.keep_until;
    $('preview-size').textContent = `${size(e.bytes)} in ${e.files.length} file${e.files.length === 1 ? '' : 's'}`;
    const list = $('preview-files');
    list.replaceChildren();
    for (const f of e.files) {
      const li = document.createElement('li');
      const k = document.createElement('span');
      k.className = `kind ${f.kind}`;
      k.textContent = f.kind;
      const n = document.createElement('span');
      n.className = 'fname';
      n.textContent = f.name;
      const z = document.createElement('span');
      z.className = 'fsize';
      z.textContent = size(f.bytes);
      li.append(k, n, z, iconLink(download(f.url), `Download ${f.name}`, ICONS.download));
      list.append(li);
    }
  }

  $('preview-play').addEventListener('click', () => {
    const v = $('preview-video');
    v.muted = false;
    v.play().catch(() => { /* the person can press play in the player */ });
  });

  const disarmPreview = armable($('preview-delete'), 'Delete? Click again', async () => {
    const e = files.events.find((x) => x.id === files.pick);
    if (!e) return;
    const response = await fetch('/api/files/delete', {
      method: 'POST',
      headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
      body: `folder=${encodeURIComponent(e.folder)}&stem=${encodeURIComponent(e.stem)}`,
    });
    if (!response.ok && response.status !== 404) throw new Error(String(response.status));
    // the next event in the list gets the preview
    const i = files.events.findIndex((x) => x.id === e.id);
    files.events.splice(i, 1);
    const shown = files.events.filter((x) => files.folder === 'all' || x.folder === files.folder);
    files.pick = shown.length ? shown[Math.min(i, shown.length - 1)].id : null;
    eventsKey = null;
    visitsKey = null;
    await loadFiles();
  });

  // ---------- Stats: disk space, space per day, clean-up
  const cleanupDone = {};     // what the last clean-up removed, shown until the page is reloaded
  async function loadStats() {
    try {
      renderStats(await getJSON('/api/stats'));
    } catch (err) { /* the next refresh tries again */ }
  }

  function renderStats(st) {
    const d = st.disk;
    const box = $('stats-disk');
    if (d) {
      box.dataset.level = d.level;
      $('stats-free').textContent = size(d.free);
      const pct = (n) => (d.total ? (100 * n) / d.total : 0);
      $('stats-pace').textContent = `${Math.round(pct(d.free))}% of ${size(d.total)}`
        + (st.days_left == null ? '' : st.days_left > 365 ? ' · enough for more than a year at this pace'
          : ` · full in about ${st.days_left < 1 ? 'less than a day' : `${Math.round(st.days_left)} days`} at this week's pace`);
      const f = d.files || {};
      const b = (k) => (f[k] ? f[k].bytes : 0);
      $('sbar-jpg').style.width = `${pct(b('jpg'))}%`;
      $('sbar-gif').style.width = `${pct(b('gif'))}%`;
      $('sbar-mp4').style.width = `${pct(b('mp4'))}%`;
      $('sbar-other').style.width = `${Math.max(0, pct(d.total - d.free - b('jpg') - b('gif') - b('mp4')))}%`;
      const legend = $('stats-legend');
      legend.replaceChildren();
      for (const [cls, name, count, bytes] of [['k-jpg', 'Pictures', f.jpg && f.jpg.count, b('jpg')],
        ['k-gif', 'GIFs', f.gif && f.gif.count, b('gif')], ['k-mp4', 'Videos', f.mp4 && f.mp4.count, b('mp4')],
        ['k-other', 'System', null, Math.max(0, d.total - d.free - d.homesec)]]) {
        const li = document.createElement('li');
        const i = document.createElement('i');
        i.className = cls;
        const v = document.createElement('b');
        v.textContent = count != null ? `${count} · ${size(bytes)}` : size(bytes);
        li.append(i, `${name} `, v);
        legend.append(li);
      }
    }
    // space per day
    const chart = $('chart');
    const labels = $('chart-labels');
    chart.replaceChildren();
    labels.replaceChildren();
    const totals = st.days.map((x) => x.jpg + x.gif + x.mp4);
    const max = Math.max(1, ...totals);
    const h = (n) => `${(n / max) * 86}%`;
    st.days.forEach((x, i) => {
      const col = document.createElement('div');
      col.className = 'col';
      const day = new Date(`${x.date}T12:00:00`);
      col.title = `${x.date}: pictures ${size(x.jpg)}, GIFs ${size(x.gif)}, videos ${size(x.mp4)}`;
      const total = document.createElement('span');
      total.className = 'total';
      total.textContent = totals[i] ? size(totals[i]) : '';
      col.append(total);
      for (const k of ['mp4', 'gif', 'jpg']) {
        const s2 = document.createElement('span');
        s2.className = `s-${k}`;
        s2.style.height = h(x[k]);
        col.append(s2);
      }
      chart.append(col);
      const l = document.createElement('span');
      l.textContent = i === 0 || day.getDate() === 1 ? `${day.getDate()} ${MONTHS[day.getMonth()]}` : String(day.getDate());
      labels.append(l);
    });
    const best = totals.indexOf(Math.max(...totals));
    $('chart-note').textContent = totals[best]
      ? `Busiest day: ${dayName(st.days[best].date, dateText(serverNow()))}, ${size(totals[best])}. Older files are deleted automatically.`
      : 'No files in the last 14 days.';
    // clean-up
    const list = $('cleanup');
    list.replaceChildren();
    const weekAgo = new Date(serverNow());
    weekAgo.setDate(weekAgo.getDate() - 7);
    const items = [
      { title: 'Rejected pictures', detail: `${st.cleanup.rejected.count} pictures where nothing was recognised · ${size(st.cleanup.rejected.bytes)}`,
        button: `Delete ${st.cleanup.rejected.count}`, off: !st.cleanup.rejected.count, body: 'action=rejected' },
      { title: 'GIFs older than 2 days', detail: `the pictures and videos stay · ${st.cleanup.old_gifs.count} GIFs · ${size(st.cleanup.old_gifs.bytes)}`,
        button: `Delete ${st.cleanup.old_gifs.count}`, off: !st.cleanup.old_gifs.count, body: 'action=old_gifs' },
      { title: 'Everything before a date', detail: 'alerts, bird visits and rejected pictures', date: dateText(weekAgo),
        button: 'Delete before', body: 'action=before' },
    ];
    for (const it of items) {
      const li = document.createElement('li');
      const text = document.createElement('div');
      const t = document.createElement('b');
      t.textContent = it.title;
      const small = document.createElement('small');
      small.textContent = it.detail;
      text.append(t, small);
      if (cleanupDone[it.body]) {
        const done = document.createElement('span');
        done.className = 'done';
        done.textContent = cleanupDone[it.body];
        text.append(done);
      }
      let input = null;
      if (it.date) {
        input = document.createElement('input');
        input.type = 'date';
        input.value = it.date;
        input.max = dateText(serverNow());
        input.setAttribute('aria-label', 'Delete everything before this date');
        text.append(document.createElement('br'), input);
      }
      const b = document.createElement('button');
      b.type = 'button';
      b.className = 'tool danger';
      b.textContent = it.button;
      b.disabled = Boolean(it.off);
      armable(b, 'Sure? Click again', async () => {
        const body = input ? `${it.body}&before=${input.value}` : it.body;
        const response = await fetch('/api/cleanup', {
          method: 'POST',
          headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
          body,
        });
        if (!response.ok) throw new Error(String(response.status));
        const gone = await response.json();
        cleanupDone[it.body] = `✓ Deleted ${gone.files} file${gone.files === 1 ? '' : 's'} · ${size(gone.bytes)}`;
        eventsKey = null;
        visitsKey = null;
        await loadStats();
      });
      li.append(text, b);
      list.append(li);
    }
    // biggest videos: a click opens them in Files
    const big = $('biggest');
    big.replaceChildren();
    if (!st.biggest.length) {
      const li = document.createElement('li');
      li.className = 'muted';
      li.textContent = 'No videos yet.';
      big.append(li);
    }
    for (const v of st.biggest) {
      const li = document.createElement('li');
      const a = document.createElement('a');
      a.href = '#files';
      a.addEventListener('click', () => { files.folder = 'all'; files.pick = v.id; files.sort = 'big'; $('files-sort').value = 'big'; });
      const left = document.createElement('span');
      left.textContent = `${v.label.charAt(0).toUpperCase()}${v.label.slice(1)} · ${dayName(v.date, dateText(serverNow()))} ${v.time.slice(0, 5)}`;
      const right = document.createElement('span');
      right.className = 'muted';
      right.textContent = size(v.bytes);
      a.append(left, right);
      li.append(a);
      big.append(li);
    }
    $('keep-note').textContent = `Kept automatically: alerts and rejected pictures ${st.keep.alerts} days, bird visits ${st.keep.visits} days `
      + '(KEEP_DAYS and VISIT_KEEP_DAYS in /etc/homesec/homesec.env).';
  }

  // ---------- the tabs: #live, #files, #stats
  const VIEWS = ['live', 'files', 'stats'];
  function showView(name) {
    view = VIEWS.includes(name) ? name : 'live';
    for (const v of VIEWS) {
      $(`view-${v}`).hidden = v !== view;
      const tab = $(`nav-${v}`);
      if (v === view) tab.setAttribute('aria-current', 'page'); else tab.removeAttribute('aria-current');
    }
    if (view === 'live') {
      startStream();
      requestAnimationFrame(() => { drawSpark(); updateArrows(); });
    } else {
      stopStream();      // the Pi makes live pictures only while someone watches
      $('preview-video').pause();
    }
    if (view === 'files') loadFiles();
    if (view === 'stats') loadStats();
  }
  window.addEventListener('hashchange', () => showView(location.hash.slice(1)));
  showView(location.hash.slice(1));

  poll();
})();
