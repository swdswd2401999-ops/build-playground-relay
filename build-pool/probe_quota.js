// 额度探测：粘到 Build App / Playground 页面的 DevTools console 里跑。
// 目的：确认这个页面到底有没有可读的额度接口 —— 这是"用完即切"能否成立的前提。
// 只读，不发请求，不改页面状态。

(() => {
  const pat = /quota|Quota|limit|Limit|usage|Usage|remain|bucket|window/i;
  const seen = new Set();

  // 1) 已经发生的资源请求里，谁长得像额度接口
  const hits = performance.getEntriesByType('resource')
    .filter(e => pat.test(e.name) && !seen.has(e.name) && seen.add(e.name));
  console.log(`[probe] 历史请求中疑似额度接口 ${hits.length} 条`);
  hits.forEach(e => console.log('  HIT', e.initiatorType, e.name));

  // 2) 页面全局对象里的可疑命名空间
  const globals = Object.keys(window).filter(k =>
    /quota|maker|suite|studio|waa|botguard/i.test(k) && typeof window[k] === 'object' && window[k]);
  console.log('[probe] 可疑全局对象:', globals);

  // 3) 从现在开始拦截 fetch / XHR，记录所有带 quota 语义的调用与响应体前 500 字
  const wrap = (fn) => function (...args) {
    const url = typeof args[0] === 'string' ? args[0] : args[0]?.url || '';
    const p = fn.apply(this, args);
    if (pat.test(url)) {
      console.log('[probe] 拦截到额度请求:', url);
      p.then(r => r.clone().text().then(t => console.log('[probe] 响应:', url, t.slice(0, 500)))).catch(() => {});
    }
    return p;
  };
  window.fetch = wrap(window.fetch);

  const origOpen = XMLHttpRequest.prototype.open;
  XMLHttpRequest.prototype.open = function (m, u, ...rest) {
    this.__probeUrl = u;
    return origOpen.call(this, m, u, ...rest);
  };
  const origSend = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.send = function (...a) {
    if (pat.test(this.__probeUrl || '')) {
      console.log('[probe] XHR 额度请求:', this.__probeUrl);
      this.addEventListener('load', () => console.log('[probe] XHR 响应:', this.__probeUrl, String(this.responseText).slice(0, 500)));
    }
    return origSend.apply(this, a);
  };

  console.log('[probe] 已布防。现在去页面里发一条消息 / 刷新一次额度面板，回来看 HIT 与拦截日志。');
  console.log('[probe] 目标：找到返回 remainingFraction / resetTime / bucketId 的那个响应。');
})();
