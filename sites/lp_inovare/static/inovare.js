(function () {
  'use strict';
  var WA = (document.body.dataset.wa || '').replace(/\D/g, '');
  var $ = function (id) { return document.getElementById(id); };
  function waUrl(t) { return WA ? 'https://wa.me/' + WA + '?text=' + encodeURIComponent(t) : ''; }
  function mask(el) {
    el.addEventListener('input', function () {
      var d = el.value.replace(/\D/g, '').slice(0, 11);
      el.value = d.length > 10 ? d.replace(/(\d{2})(\d{5})(\d{0,4})/, '($1) $2-$3')
        : d.length > 6 ? d.replace(/(\d{2})(\d{4})(\d{0,4})/, '($1) $2-$3')
        : d.length > 2 ? d.replace(/(\d{2})(\d{0,5})/, '($1) $2') : d;
    });
  }
  function postLead(obj) {
    var fd = new FormData();
    Object.keys(obj).forEach(function (k) { if (obj[k]) fd.append(k, obj[k]); });
    fd.append('pagina', location.href);
    if (document.referrer) fd.append('referrer', document.referrer);
    var q = new URLSearchParams(location.search);
    ['utm_source', 'utm_medium', 'utm_campaign', 'utm_term', 'utm_content'].forEach(function (k) { if (q.get(k)) fd.append(k, q.get(k)); });
    return fetch('/lead', { method: 'POST', body: fd, headers: { 'X-Requested-With': 'XMLHttpRequest', 'Accept': 'application/json' } })
      .then(function (r) { return r.json().catch(function () { return {}; }).then(function (j) { return { ok: r.ok && j.ok !== false, msg: j.erro || j.mensagem }; }); })
      .catch(function () { return { ok: false, msg: 'Falha de conexão. Tente novamente.' }; });
  }

  // menu mobile + ano
  var mb = document.querySelector('.menu-toggle'), nav = document.querySelector('.main-nav');
  if (mb && nav) {
    mb.addEventListener('click', function () {
      var o = nav.classList.toggle('open');
      mb.setAttribute('aria-expanded', String(o)); mb.setAttribute('aria-label', o ? 'Fechar menu' : 'Abrir menu');
    });
    nav.querySelectorAll('a').forEach(function (a) { a.addEventListener('click', function () { nav.classList.remove('open'); mb.setAttribute('aria-expanded', 'false'); }); });
  }
  var yr = $('year'); if (yr) yr.textContent = new Date().getFullYear();

  // formulário de orçamento -> /lead (painel) + WhatsApp
  var form = $('contact-form');
  if (form) {
    mask(form.elements.telefone);
    var rules = {
      nome: function (v) { return v.trim().length >= 2 || 'Informe seu nome.'; },
      telefone: function (v) { return /^\d{10,11}$/.test(v.replace(/\D/g, '')) || 'Informe um WhatsApp válido com DDD.'; },
      email: function (v) { return /^[^\s@]+@[^\s@]+\.[^\s@]{2,}$/.test(v.trim()) || 'Informe um e-mail válido.'; },
      interesse: function (v) { return v !== '' || 'Selecione uma opção.'; }
    };
    var setError = function (el, msg) {
      var f = el.closest('.field'); f.classList.toggle('invalid', !!msg);
      var e = f.querySelector('.error'); if (e) e.textContent = msg || '';
      el.setAttribute('aria-invalid', msg ? 'true' : 'false');
    };
    var check = function (el) {
      var r = true;
      if (el.name === 'consentimento') r = el.checked || 'Para enviar, confirme o consentimento.';
      else if (rules[el.name]) r = rules[el.name](el.value);
      setError(el, r === true ? '' : r); return r === true;
    };
    var required = [].slice.call(form.querySelectorAll('[required]'));
    required.forEach(function (el) {
      ['blur', 'input', 'change'].forEach(function (ev) {
        el.addEventListener(ev, function () { if (ev === 'blur' || el.closest('.field').classList.contains('invalid')) check(el); });
      });
    });
    var btn = form.querySelector('.form-submit');
    form.addEventListener('submit', function (e) {
      e.preventDefault();
      var res = required.map(check);
      if (res.indexOf(false) >= 0) { required[res.indexOf(false)].focus(); return; }
      var f = {}; new FormData(form).forEach(function (v, k) { f[k] = v; });
      var err = form.querySelector('.form-erro'); if (err) err.remove();
      var win = WA ? window.open('', '_blank') : null;
      btn.disabled = true;
      var linhas = ['Olá! Quero solicitar um orçamento.', '', '*Nome:* ' + f.nome, f.empresa && '*Empresa:* ' + f.empresa,
        '*WhatsApp:* ' + f.telefone, '*E-mail:* ' + f.email, '*Interesse:* ' + f.interesse,
        f.quantidade && '*Quantidade:* ' + f.quantidade, f.mensagem && '*Mensagem:* ' + f.mensagem].filter(Boolean);
      postLead(f).then(function (r) {
        btn.disabled = false;
        if (!r.ok) {
          if (win) win.close();
          var p = document.createElement('p'); p.className = 'form-erro'; p.setAttribute('role', 'alert');
          p.textContent = r.msg || 'Não foi possível enviar agora. Tente novamente.'; btn.insertAdjacentElement('afterend', p); return;
        }
        var url = waUrl(linhas.join('\n'));
        if (win && url) win.location.href = url;
        var fb = $('wa-fallback'); if (fb) { if (url) fb.href = url; else fb.parentNode.innerHTML = '<strong>Recebemos seu contato!</strong> Em breve retornaremos.'; }
        $('form-success').hidden = false;
        form.reset(); required.forEach(function (el) { setError(el, ''); });
      });
    });
  }

  // widget flutuante do WhatsApp
  var root = $('wa-widget');
  if (root) {
    var fab = $('wa-fab'), panel = $('wa-panel'), bubble = $('wa-bubble'), wf = $('wa-form'),
        nome = $('wa-nome'), tel = $('wa-tel'), ok = $('wa-ok'), hideT;
    var mark = document.querySelector('.brand-mark'); if (mark) $('wa-avatar-img').src = mark.src;
    var hideBubble = function () { bubble.hidden = true; clearTimeout(bt); };
    var open = function () { hideBubble(); panel.hidden = false; root.classList.add('open'); fab.setAttribute('aria-expanded', 'true'); setTimeout(function () { nome.focus({ preventScroll: true }); }, 60); };
    var close = function () { panel.hidden = true; root.classList.remove('open'); fab.setAttribute('aria-expanded', 'false'); };
    var bt = setTimeout(function () { if (panel.hidden) bubble.hidden = false; }, 6000);
    fab.addEventListener('click', function () { panel.hidden ? open() : close(); });
    $('wa-bubble-x').addEventListener('click', hideBubble);
    bubble.addEventListener('click', function (e) { if (e.target.id !== 'wa-bubble-x') open(); });
    document.addEventListener('keydown', function (e) { if (e.key === 'Escape' && !panel.hidden) { close(); fab.focus(); } });
    document.addEventListener('click', function (e) {
      if (!panel.hidden && !root.contains(e.target)) close();
      if (e.target.closest('[data-wa-open]')) { e.preventDefault(); open(); }
    });
    mask(tel);
    var setErr = function (el, m) { var f = el.closest('.wa-field'); f.classList.toggle('invalid', !!m); f.querySelector('.wa-err').textContent = m || ''; };
    [nome, tel].forEach(function (el) { el.addEventListener('input', function () { setErr(el, ''); }); });
    wf.addEventListener('submit', function (e) {
      e.preventDefault();
      var okN = nome.value.trim().length >= 2, okT = /^\d{10,11}$/.test(tel.value.replace(/\D/g, ''));
      setErr(nome, okN ? '' : 'Informe seu nome.'); setErr(tel, okT ? '' : 'Informe um WhatsApp válido com DDD.');
      if (!okN || !okT) { (okN ? tel : nome).focus(); return; }
      var ctx = document.body.dataset.contexto;
      var texto = 'Olá! Meu nome é ' + nome.value.trim() + ' e meu WhatsApp é ' + tel.value + '. Gostaria de solicitar um orçamento.' + (ctx ? '\n(Estou vendo: ' + ctx + ')' : '');
      var win = WA ? window.open('', '_blank') : null;
      postLead({ nome: nome.value.trim(), telefone: tel.value, mensagem: texto, origem: 'Botão flutuante WhatsApp' }).then(function () {
        var url = waUrl(texto);
        if (win && url) win.location.href = url;
        var fb = $('wa-fallback2'); if (fb && url) fb.href = url;
        ok.hidden = false; wf.reset();
        clearTimeout(hideT); hideT = setTimeout(function () { close(); ok.hidden = true; }, 5000);
      });
    });
  }

  // carrossel de trabalhos
  var track = document.querySelector('.portfolio-track');
  [['.slider-arrow.next', 0.75], ['.slider-arrow.prev', -0.75]].forEach(function (a) {
    var b = document.querySelector(a[0]);
    if (b && track) b.addEventListener('click', function () { track.scrollBy({ left: track.clientWidth * a[1], behavior: 'smooth' }); });
  });

  // categoria: filtros + botão "Orçamento" do produto
  var filtros = $('cat-filters'), lista = $('cat-products');
  if (filtros && lista) filtros.addEventListener('click', function (e) {
    var b = e.target.closest('.chip'); if (!b) return;
    filtros.querySelectorAll('.chip').forEach(function (c) { c.setAttribute('aria-pressed', String(c === b)); });
    var f = b.dataset.filtro;
    lista.querySelectorAll('.pcard').forEach(function (c) { c.hidden = f !== 'Todos' && c.dataset.tipo !== f; });
  });
  if (lista) lista.addEventListener('click', function (e) {
    var b = e.target.closest('button.pbtn'); if (!b) return;
    var msg = $('cf-msg'); if (msg) msg.value = 'Tenho interesse em: ' + b.dataset.produto + '.\n';
    $('contato').scrollIntoView({ behavior: 'smooth' });
    setTimeout(function () { var n = $('cf-nome'); if (n) n.focus({ preventScroll: true }); }, 600);
  });

  // produto: galeria
  var gm = $('g-main'), gt = document.querySelector('.g-thumbs');
  if (gm && gt) gt.addEventListener('click', function (e) {
    var b = e.target.closest('.g-thumb'); if (!b) return;
    var i = +b.dataset.i;
    gt.querySelectorAll('.g-thumb').forEach(function (t) { t.setAttribute('aria-current', String(t === b)); });
    gm.className = 'g-main t' + (i % 6 + 1);
    var old = gm.querySelector('img'); if (old) old.remove();
    if (b.dataset.src) { var im = document.createElement('img'); im.src = b.dataset.src; im.alt = ''; im.onerror = function () { im.remove(); }; gm.appendChild(im); }
  });
})();
