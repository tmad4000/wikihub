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
  // Match a unique occurrence in the current article, including inline formatting.
  function locate(text) {
    const walker = document.createTreeWalker(article, NodeFilter.SHOW_TEXT);
    const nodes = [];
    let s = '', n;
    while ((n = walker.nextNode())) {
      nodes.push({n, start:s.length});
      s += n.data;
    }
    const i = s.indexOf(text), j = i + text.length;
    if (i < 0 || i !== s.lastIndexOf(text)) return null;
    const first = nodes.find(x => x.start + x.n.length > i);
    const last = nodes.find(x => x.start + x.n.length >= j);
    if (!first || !last) return null;
    const r = new Range();
    r.setStart(first.n, i-first.start);
    r.setEnd(last.n, j-last.start);
    return r;
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
  async function refresh() {
    try {
      const res = await fetch(root.dataset.url,{cache:'no-store',headers:{Accept:'application/json','X-CSRFToken':get('token').value}});
      if (!res.ok) throw new Error();
      const data = await res.json();
      list.replaceChildren(...data.comments.map(showComment));
      status.textContent = data.comments.length ? '' : 'No comments yet.';
    } catch { status.textContent = 'Comments could not be loaded.'; }
  }
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
  function close() { form.hidden = true; if(page) page.hidden = true; }
  function compose(anchor,body) {
    close(); quote=anchor; issuedPayload=''; requestId=crypto.randomUUID();
    get('heading').textContent=body ? 'Review suggested comment' : 'New comment';
    get('destination').textContent=target+' · Visible to readers with access to this content';
    get('quote').textContent=quote;get('quote').hidden=!quote;
    get('body').value=body;form.hidden=false;
    form.scrollIntoView({block:'center'});get('body').focus();
  }
  get('compose').onclick=()=>compose('','');
  get('selection').onclick=()=>compose(selected,'');
  root.querySelectorAll('[data-cancel]').forEach(b=>b.onclick=close);
  root.addEventListener('review-reader-draft',e=>{
    if(e.detail.type==='comment') compose(e.detail.quote || '',e.detail.body);
    else if(page) {
      close();page.elements.content.value=e.detail.body;page.elements.path.value='';
      page.elements.request_id.value=crypto.randomUUID();page.hidden=false;
      page.scrollIntoView({block:'center'});page.elements.path.focus();
    }
  });
  form.onsubmit=async e=>{
    e.preventDefault();
    const submit=form.querySelector('[type=submit]');
    if(submit.disabled)return;
    const body=get('body').value.trim();if(!body)return;
    if(body.length>2000){status.textContent='Shorten this comment to 2,000 characters before approving.';return;}
    if(issuedPayload && issuedPayload!==body) requestId=crypto.randomUUID();
    issuedPayload=body;submit.disabled=true;
    try {
      const res=await fetch(root.dataset.url,{method:'POST',headers:{'Content-Type':'application/json','X-CSRFToken':get('token').value},body:JSON.stringify({body,quote,request_id:requestId,revision:root.dataset.revision,viewer_id:root.dataset.viewer})});
      if(!res.ok){const d=await res.json().catch(()=>({}));throw new Error(d.message||'Posting failed ('+res.status+').');}
      close();await refresh();
    } catch(err){status.textContent=err.message;}
    finally{submit.disabled=false;}
  };
})();
