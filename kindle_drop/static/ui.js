let token = location.hash.slice(1) || sessionStorage.getItem('kindle-drop-token') || '';
let info = {};
const el = id => document.getElementById(id);
function message(text, error = false) { el('message').textContent = text; el('message').className = error ? 'error' : ''; }
async function api(path, body, type = 'application/json') {
  const headers = { 'X-Kindle-Token': token };
  if (body !== undefined) headers['Content-Type'] = type;
  const response = await fetch(path, { method: body === undefined ? 'GET' : 'POST', headers, body });
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || `HTTP ${response.status}`);
  return result;
}
async function refresh() {
  info = await api('/api/info');
  el('connection').textContent = info.send_ready ? `Kindle paired · ${info.kindle.host}:${info.kindle.port} · Destination: ${info.kindle.inbox} · Keep KOReader SSH awake to send.` : `${info.send_blocked_reason || 'Setup pending.'} You can still add readings.`;
  const books = await api('/api/books');
  el('books').replaceChildren();
  if (!books.length) { const empty = document.createElement('div'); empty.className = 'empty'; empty.textContent = 'Your next read starts with a link or an EPUB.'; el('books').append(empty); }
  for (const book of books) {
    const row = document.createElement('article'); row.className = 'book';
    const text = document.createElement('div'); text.className = 'book-text';
    const title = document.createElement('h3'); title.className = 'book-title'; title.textContent = book.title;
    const remove = document.createElement('button'); remove.className = 'book-remove'; remove.textContent = '×'; remove.type = 'button';
    const heading = document.createElement('div'); heading.className = 'book-heading'; heading.append(remove, title);
    const meta = document.createElement('div'); meta.className = 'book-meta';
    const status = document.createElement('span'); status.className = 'status'; status.textContent = book.status;
    meta.append(status, document.createTextNode(`${book.author} · ${Math.ceil(book.size / 1024)} KB${book.images ? ` · ${book.images} images` : ''}`)); text.append(heading, meta);
    if (book.remote_path) { const delivery = document.createElement('p'); delivery.className = 'book-meta delivery'; delivery.textContent = `On Kindle: ${book.remote_path}`; text.append(delivery); }
    if (book.warnings && book.warnings.length) { const warning = document.createElement('p'); warning.className = 'book-meta'; warning.textContent = book.warnings.join(' '); text.append(warning); }
    const actions = document.createElement('div'); actions.className = 'book-actions';
    const download = document.createElement('a'); download.className = 'secondary download';
    download.textContent = 'Download EPUB'; download.href = `${info.download_path}/${book.id}`;
    download.download = book.filename; download.title = 'Download and review this EPUB before sending it.';
    const send = document.createElement('button'); send.textContent = book.status === 'Sent' ? 'Send again' : 'Send to Kindle';
    send.disabled = !info.send_ready;
    if (send.disabled) send.title = info.send_blocked_reason || 'Complete Kindle setup before sending.';
    send.onclick = async () => {
      send.disabled = true; remove.disabled = true; send.setAttribute('aria-busy', 'true'); send.textContent = 'Sending…';
      message('Sending and verifying the EPUB…');
      try { const result = await api(`/api/books/${book.id}/send`, '{}'); message(result.message); await refresh(); }
      catch (error) { message(error.message, true); }
      finally { send.removeAttribute('aria-busy'); send.textContent = book.status === 'Sent' ? 'Send again' : 'Send to Kindle'; send.disabled = !info.send_ready; remove.disabled = false; }
    };
    remove.title = 'Remove this EPUB from the PC queue. Copies on the Kindle are kept.';
    remove.setAttribute('aria-label', `Remove ${book.title} from reading queue`);
    remove.onclick = async () => {
      remove.disabled = true; send.disabled = true; remove.setAttribute('aria-busy', 'true');
      try { const result = await api(`/api/books/${book.id}/remove`, '{}'); message(result.message); await refresh(); }
      catch (error) { message(error.message, true); }
      finally { remove.disabled = false; send.disabled = !info.send_ready; remove.removeAttribute('aria-busy'); }
    };
    actions.append(download, send); row.append(text, actions); el('books').append(row);
  }
  el('links').replaceChildren();
  if (info.lan) for (const [label, links] of [['Phone', info.mobile], ['KOReader OPDS', info.opds]]) for (const href of links) {
    const a = document.createElement('a'); a.href = href; a.textContent = `${label}: ${href}`; el('links').append(a);
  }
}
async function addFiles(files) {
  for (const file of files) {
    try { message(`Adding ${file.name}…`); if (!file.name.toLowerCase().endsWith('.epub')) throw new Error('Choose .epub files.'); if (file.size > 40 * 1024 * 1024) throw new Error('The EPUB exceeds the 40 MB limit.'); await api('/api/upload', file, 'application/epub+zip'); message('Added to your reading queue.'); await refresh(); } catch (error) { message(error.message, true); }
  }
}
el('url-form').onsubmit = async event => {
  event.preventDefault(); const button = event.target.querySelector('button'); button.disabled = true; message('Fetching your link and preparing an EPUB…');
  try { await api('/api/url', JSON.stringify({ url: el('url').value.trim() })); el('url').value = ''; message('Added to your reading queue.'); await refresh(); } catch (error) { message(error.message, true); } finally { button.disabled = false; }
};
el('files').onchange = event => addFiles(event.target.files);
el('refresh').onclick = () => refresh().catch(error => message(error.message, true));
el('drop').ondragover = event => { event.preventDefault(); el('drop').classList.add('active'); };
el('drop').ondragleave = () => el('drop').classList.remove('active');
el('drop').ondrop = event => { event.preventDefault(); el('drop').classList.remove('active'); addFiles(event.dataTransfer.files); };
(async () => {
  try {
    if (location.hostname === '127.0.0.1' || location.hostname === 'localhost') { const response = await fetch('/api/bootstrap'); if (!response.ok) throw new Error('Could not authenticate the local browser.'); token = (await response.json()).token; }
    if (!token) throw new Error('Open the private phone link shown on your PC.');
    sessionStorage.setItem('kindle-drop-token', token); history.replaceState(null, '', location.pathname); await refresh();
  } catch (error) { message(error.message, true); el('connection').textContent = 'Could not connect to the local inbox.'; }
})();
