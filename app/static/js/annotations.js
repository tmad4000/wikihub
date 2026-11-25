(() => {
  const root = document.querySelector('#page-comments');
  if (!root) return;
  const article = document.querySelector('article.article');
  const get = name => root.querySelector('[data-' + name + ']');
  const form = get('comment-form'), page = get('page-form'), status = get('status');
  const list = get('comments'), target = root.dataset.target;
  let selected = '', quote = '', requestId, issuedPayload = '';
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
    item.append(node('p',c.body));
    return item;
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
  const submit = form.querySelector('[type=submit]');
  let audience, composition = 0;
  const labels = {
    owner_only: 'Owner only', private: 'Readers with access to this private page',
    unlisted: 'Anyone with the link', public: 'Public',
  };
  function showAudience(value) {
    if (!Object.hasOwn(labels, value)) throw new Error('Could not verify the audience.');
    audience = value;
    get('destination').textContent = target + ' · ' + labels[value];
  }
  function close() { composition++; audience = null; form.hidden = true; if(page) page.hidden = true; }
  function post(url, data) {
    return fetch(url, {method:'POST', headers:{'Content-Type':'application/json','X-CSRFToken':get('token').value},
      body:JSON.stringify({...data, quote, revision:root.dataset.revision, viewer_id:Number(root.dataset.viewer)})});
  }
  async function compose(anchor,body) {
    close(); quote=anchor; issuedPayload=''; requestId=crypto.randomUUID();
    const current = composition;
    submit.disabled = true;
    get('heading').textContent=body ? 'Review suggested comment' : 'New comment';
    get('destination').textContent = target + ' · Checking audience…';
    get('quote').textContent=quote;get('quote').hidden=!quote;
    get('body').value=body;form.hidden=false;
    form.scrollIntoView({block:'center'});get('body').focus();
    try {
      const res = await post(root.dataset.url + '?preview=1', {});
      const result = await res.json();
      if (current !== composition) return;
      if (!res.ok) throw new Error(result.message || 'Could not verify the audience.');
      showAudience(result.audience);
      submit.disabled = false;
    } catch (err) {
      if (current === composition) get('destination').textContent = target + ' · ' + err.message;
    }
  }
  get('compose').onclick=()=>compose('','');
  get('selection').onclick=()=>compose(selected,'');
  root.querySelectorAll('[data-cancel]').forEach(b=>b.onclick=close);
  root.addEventListener('review-reader-draft',e=>{
    if(e.detail.type==='comment') compose(e.detail.quote || '',e.detail.body);
    else if(page) {
      close();page.elements.content.value=e.detail.body;page.elements.path.value='';
      page.elements.path.dispatchEvent(new Event('input'));
      page.elements.request_id.value=crypto.randomUUID();page.hidden=false;
      page.scrollIntoView({block:'center'});page.elements.path.focus();
    }
  });
  if (page) {
    const pathInput = page.elements.path;
    const approve = page.querySelector('[type=submit]');
    const check = get('check-target');
    if (check) {
      pathInput.oninput = () => {
        approve.disabled = true;
        page.elements.previewed_visibility.value = '';
        get('audience').textContent = 'Choose a filename to check visibility';
      };
      check.onclick = async () => {
        const path = pathInput.value.trim();
        const url = new URL(page.action);
        url.searchParams.set('assistant_target', path);
        try {
          const res = await fetch(url.toString(), {cache:'no-store'});
          const result = await res.json();
          if (path !== pathInput.value.trim()) return;
          if (!res.ok) throw new Error(result.message || 'Cannot use this destination.');
          pathInput.value = result.path;
          page.elements.previewed_visibility.value = result.visibility;
          get('audience').textContent = result.visibility;
          approve.disabled = false;
        } catch (err) { status.textContent = err.message; }
      };
    }
    page.onsubmit = async e => {
      e.preventDefault();
      if (approve.disabled) return;
      approve.disabled = true;
      try {
        const res = await fetch(page.action, {method:'POST', headers:{Accept:'application/json'},
          body:new FormData(page)});
        const result = await res.json();
        if (!res.ok) throw new Error(result.message || 'Could not create the page.');
        location.assign(result.url);
      } catch (err) {
        status.textContent = err.message;
        approve.disabled = false;
      }
    };
  }
  form.onsubmit=async e=>{
    e.preventDefault();
    if(submit.disabled || !audience)return;
    const current = composition;
    const body = get('body').value;
    if (!body.trim()) return;
    if(body.length>4000){status.textContent='Shorten this comment to 4,000 characters before approving.';return;}
    if(issuedPayload && issuedPayload!==body) requestId=crypto.randomUUID();
    issuedPayload=body;submit.disabled=true;
    try {
      const res = await post(root.dataset.url, {body, request_id:requestId, previewed_audience:audience});
      if (!res.ok) {
        const d = await res.json().catch(()=>({}));
        if (current !== composition) return;
        if (d.error === 'audience_changed') showAudience(d.audience);
        throw new Error(d.message || 'Posting failed ('+res.status+').');
      }
      if (current === composition) close();
      await refresh();
    } catch(err){if (current === composition) status.textContent=err.message;}
    finally{if (current === composition) submit.disabled=false;}
  };
})();
