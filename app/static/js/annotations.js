(() => {
  const root = document.querySelector('#page-comments');
  if (!root) return;
  const article = document.querySelector('article.article');
  const get = name => root.querySelector('[data-' + name + ']');
  const form = get('comment-form'), page = get('page-form'), status = get('status');
  const list = get('comments'), target = root.dataset.target;
  let selected = '', quote = '', requestId, issuedPayload = '', audience = null, citations = [];
  function node(tag, value) {
    const n = document.createElement(tag);
    n.textContent = value;
    return n;
  }
  const blocks = new Set('address article aside blockquote br dd details div dl dt figcaption figure footer h1 h2 h3 h4 h5 h6 header hr li main nav ol p pre section summary table tbody td tfoot th thead tr ul'.split(' '));
  const space = /[\p{White_Space}\u001c-\u001f]/u;
  let articleIndex;
  function buildIndex() {
    let text = '', pendingSpace = false;
    const nodes = [], offsets = [];
    function visit(n) {
      if (n.nodeType === Node.TEXT_NODE) {
        for (let i = 0; i < n.length; i++) {
          if (space.test(n.data[i])) {
            pendingSpace = true;
            continue;
          }
          if (pendingSpace && text.length) {
            text += ' ';
            nodes.push(null);
            offsets.push(0);
          }
          pendingSpace = false;
          text += n.data[i];
          nodes.push(n);
          offsets.push(i);
        }
      } else if (n.nodeType === Node.ELEMENT_NODE) {
        if (n.matches('script,style,template,noscript,.private-band-label')) return;
        const block = blocks.has(n.localName);
        if (block) pendingSpace = true;
        n.childNodes.forEach(visit);
        if (block) pendingSpace = true;
      }
    }
    visit(article);
    return {text, nodes, offsets};
  }
  if (article) new MutationObserver(() => { articleIndex = null; })
    .observe(article, {subtree:true, childList:true, characterData:true});
  function locate(quote) {
    if (!article) return null;
    const {text, nodes, offsets} = articleIndex ??= buildIndex();
    const needle = quote.replace(/[\p{White_Space}\u001c-\u001f]+/gu, ' ').replace(/^ | $/g, '');
    const start = text.indexOf(needle);
    if (!needle || start < 0 || start !== text.lastIndexOf(needle)) return null;
    const end = start + needle.length - 1;
    const range = new Range();
    range.setStart(nodes[start], offsets[start]);
    range.setEnd(nodes[end], offsets[end] + 1);
    return range;
  }
  function showComment(c) {
    const item = document.createElement('li');
    item.append(node('strong','@' + c.author), node('small',c.owner_only ? ' · Owner only' : ''));
    if (c.quote) {
      item.append(node('blockquote', c.quote));
      const range = !c.detached && locate(c.quote);
      if (range) {
        const jump = node('button','Show passage');
        jump.className = 'action-btn';
        jump.onclick = () => {
          range.startContainer.parentElement.scrollIntoView({block:'center'});
          if (window.CSS?.highlights) CSS.highlights.set('wikihub-comment',new Highlight(range));
          else { const s = getSelection(); s.removeAllRanges(); s.addRange(range); }
        };
        item.append(jump);
      } else item.append(node('small','Passage changed or appears more than once.'));
    }
    item.append(bodyWithCitations(c.body, c.citations || []));
    return item;
  }
  // Server-resolved citations: only pages this viewer can read, same-site
  // paths only. [n] markers in the body become links; built with DOM APIs.
  function safePath(url) {
    return typeof url === 'string' && url.startsWith('/@') && !url.includes('\\') ? url : null;
  }
  function bodyWithCitations(body, cites) {
    const p = document.createElement('p');
    const byN = new Map(cites.filter(c => safePath(c.url)).map(c => [c.n, c]));
    let last = 0;
    for (const m of body.matchAll(/\[(\d{1,2})\]/g)) {
      const c = byN.get(Number(m[1]));
      if (!c) continue;
      p.append(body.slice(last, m.index));
      const a = node('a', m[0]); a.href = safePath(c.url); a.className = 'comment-ref';
      a.title = c.title + (c.quote ? ' — “' + c.quote + '”' : '');
      p.append(a); last = m.index + m[0].length;
    }
    p.append(body.slice(last));
    if (!byN.size) return p;
    const wrap = document.createElement('div');
    const list = document.createElement('ol'); list.className = 'comment-sources';
    for (const c of [...byN.values()].sort((a, b) => a.n - b.n)) {
      const li = document.createElement('li'); li.value = c.n;
      const a = node('a', c.title); a.href = safePath(c.url); li.append(a);
      if (c.quote) li.append(' ', node('q', c.quote));
      list.append(li);
    }
    wrap.append(p, list);
    return wrap;
  }
  const older = get('older');
  let before = null, loadSequence = 0;
  async function refresh(loadOlder = false) {
    const sequence = ++loadSequence;
    older.disabled = true;
    try {
      const url = new URL(root.dataset.url, location.href);
      if (loadOlder) url.searchParams.set('before', before);
      const res = await fetch(url,{cache:'no-store',headers:{Accept:'application/json','X-CSRFToken':get('token').value}});
      if (!res.ok) throw new Error();
      const data = await res.json();
      if (sequence !== loadSequence) return;
      const items = data.comments.map(showComment);
      if (loadOlder) list.prepend(...items);
      else list.replaceChildren(...items);
      before = data.before;
      older.hidden = !before;
      status.textContent = list.childElementCount || before ? '' : 'No comments yet.';
    } catch {
      if (sequence === loadSequence) status.textContent = 'Comments could not be loaded.';
    } finally {
      if (sequence === loadSequence) older.disabled = false;
    }
  }
  older.onclick = () => refresh(true);
  refresh();
  if (!form) return;
  function capture() {
    const s = getSelection();
    if (!s.rangeCount || s.isCollapsed) return;
    if (!article.contains(s.getRangeAt(0).commonAncestorContainer)) return;
    selected = s.toString().trim();
    get('selection').hidden = !selected;
    if (selected) get('selection').title = selected.slice(0,120);
  }
  document.addEventListener('selectionchange',capture);
  let composition = 0;
  function close() { composition++; form.hidden = true; if(page) page.hidden = true; }
  const submitComment = form.querySelector('[type=submit]');
  async function previewComment() {
    // Exact audience + canonical page from the server, before approval.
    const current = composition;
    audience = null; submitComment.disabled = true;
    get('destination').textContent = 'Checking…';
    try {
      const res = await fetch(root.dataset.url, {method:'POST', headers:{'Content-Type':'application/json','X-CSRFToken':get('token').value},
        body: JSON.stringify({preview:true, quote, revision:root.dataset.revision, viewer_id:Number(root.dataset.viewer)})});
      const d = await res.json().catch(() => ({}));
      if (current !== composition) return;
      if (!res.ok) throw new Error(d.message || 'This comment cannot be posted here.');
      audience = d.preview.audience;
      get('destination').textContent = d.preview.audience_label;
      get('page-ref').textContent = d.preview.page_path;
      submitComment.disabled = false;
    } catch (err) { if(current === composition) get('destination').textContent = err.message; }
  }
  function showDraftSources() {
    const list = get('draft-sources');
    list.replaceChildren(...citations.map(c => { const li = node('li', c.title || c.url); li.value = c.n; return li; }));
    list.hidden = !citations.length;
  }
  function compose(anchor,body,cites) {
    close(); quote=anchor; issuedPayload=''; requestId=crypto.randomUUID();
    citations = (cites || []).filter(c => safePath(c.url)).map(c => ({n:c.n, url:c.url, title:c.title}));
    get('heading').textContent=body ? 'Review suggested comment' : 'New comment';
    get('quote').textContent=quote;get('quote').hidden=!quote;
    get('body').value=body;form.hidden=false;showDraftSources();
    form.scrollIntoView({block:'center'});get('body').focus();
    previewComment();
  }
  get('compose').onclick=()=>compose('','');
  get('selection').onclick=()=>compose(selected,'');
  root.querySelectorAll('[data-cancel]').forEach(b=>b.onclick=close);
  root.addEventListener('review-reader-draft',e=>{
    if(e.detail.type==='comment') compose(e.detail.quote || '',e.detail.body,e.detail.citations);
    else if(page) {
      close();page.elements.content.value=e.detail.body;page.elements.path.value='';
      page.elements.path.dispatchEvent(new Event('input'));
      page.elements.request_id.value=crypto.randomUUID();page.hidden=false;
      page.scrollIntoView({block:'center'});page.elements.path.focus();
    }
  });
  if (page) {
    const pathInput = page.elements.path, contentInput = page.elements.content;
    const approve = page.querySelector('[type=submit]');
    const reset = () => {
      approve.disabled = true;
      page.elements.previewed_visibility.value = '';
      page.elements.previewed_content_hash.value = '';
      get('final-review').hidden = true;
    };
    pathInput.oninput = reset; contentInput.oninput = reset;
    function showPreview(p) {
      pathInput.value = p.path;
      page.elements.previewed_visibility.value = p.visibility;
      page.elements.previewed_content_hash.value = p.content_hash;
      get('final-path').textContent = p.page_path;
      get('audience').textContent = p.audience_label;
      get('final-markdown').textContent = p.content;  // exactly what will be saved
      get('final-review').hidden = false;
      approve.disabled = false;
    }
    get('check-target').onclick = async () => {
      reset();
      const data = new FormData(page);
      data.set('preview', '1'); data.delete('approved');
      try {
        const res = await fetch(page.action, {method:'POST', headers:{Accept:'application/json'}, body:data});
        const result = await res.json().catch(() => ({}));
        if (!res.ok) throw new Error(result.message || 'Cannot use this destination.');
        showPreview(result.preview);
        status.textContent = '';
      } catch (err) { status.textContent = err.message; }
    };
    page.onsubmit = async e => {
      e.preventDefault();
      if (approve.disabled) return;
      approve.disabled = true;
      try {
        const res = await fetch(page.action, {method:'POST', headers:{Accept:'application/json'},
          body:new FormData(page)});
        const result = await res.json().catch(() => ({}));
        if (res.status === 409 && result.error === 'preview_changed' && result.preview) {
          showPreview(result.preview);
          approve.disabled = false;
          throw new Error('The page changed from what you reviewed (shown above). Approve again to save it.');
        }
        if (!res.ok) throw new Error(result.message || 'Could not create the page.');
        location.assign(result.url);
      } catch (err) {
        status.textContent = err.message;
        if (!page.elements.previewed_content_hash.value) approve.disabled = true;
        else approve.disabled = false;
      }
    };
  }
  form.onsubmit=async e=>{
    e.preventDefault();
    const submit=form.querySelector('[type=submit]');
    if(submit.disabled)return;
    const body=get('body').value;if(!body.trim())return;
    if(body.length>4000){status.textContent='Shorten this comment to 4,000 characters before approving.';return;}
    if(!audience)return;
    if(issuedPayload && issuedPayload!==body) requestId=crypto.randomUUID();
    issuedPayload=body;submit.disabled=true;
    let keepDisabled=false;
    try {
      const res=await fetch(root.dataset.url,{method:'POST',headers:{'Content-Type':'application/json','X-CSRFToken':get('token').value},
        body:JSON.stringify({body,quote,request_id:requestId,revision:root.dataset.revision,viewer_id:Number(root.dataset.viewer),
          audience,citations:citations.map(c=>({n:c.n,url:c.url}))})});
      const d=await res.json().catch(()=>({}));
      if(res.status===409&&d.error==='audience_changed'){
        audience=d.audience;get('destination').textContent=d.audience_label;requestId=crypto.randomUUID();issuedPayload='';
        throw new Error('Who can read this comment changed (shown above). Approve again to post.');
      }
      if(!res.ok){if(d.error==='viewer_mismatch'||d.error==='stale_revision')keepDisabled=true;throw new Error(d.message||'Posting failed ('+res.status+').');}
      close();await refresh();
    } catch(err){status.textContent=err.message;}
    finally{submit.disabled=keepDisabled;}
  };
})();
