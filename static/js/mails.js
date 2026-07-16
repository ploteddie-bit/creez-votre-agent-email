/* mails.js — liste paginée des emails via GET /api/emails. */
(function () {
  'use strict';
  var AM = window.AM;
  var LIMIT = 25;
  var state = { offset: 0, sender: '', label: '' };

  function readFiltersFromUrl() {
    state.sender = AM.queryParam('sender') || '';
    state.label = AM.queryParam('label') || '';
    var off = parseInt(AM.queryParam('offset'), 10);
    state.offset = isNaN(off) || off < 0 ? 0 : off;
    AM.$('f-sender').value = state.sender;
    AM.$('f-label').value = state.label;
  }

  function pushUrl() {
    var p = new URLSearchParams();
    if (state.sender) p.set('sender', state.sender);
    if (state.label) p.set('label', state.label);
    if (state.offset) p.set('offset', String(state.offset));
    var qs = p.toString();
    window.history.replaceState(null, '', '/mails' + (qs ? '?' + qs : ''));
  }

  function labelsBadges(labels) {
    if (!labels || !labels.length) return '<span class="muted">—</span>';
    return labels.slice(0, 3).map(function (l) {
      return '<span class="badge">' + AM.escapeHtml(l) + '</span>';
    }).join(' ') + (labels.length > 3 ?
      ' <span class="muted">+' + (labels.length - 3) + '</span>' : '');
  }

  function stateBadges(row) {
    var bits = [];
    if (row.is_read) bits.push('<span class="badge">lu</span>');
    else bits.push('<span class="badge badge--accent">non lu</span>');
    if (row.is_starred) bits.push('<span class="badge badge--warn">★</span>');
    return bits.join(' ');
  }

  function renderRows(data) {
    var tbody = AM.$('mails-body');
    if (!data.items || data.items.length === 0) {
      tbody.innerHTML = '<tr><td colspan="6"><div class="state">' +
        'Aucun email ne correspond aux filtres. La base est peut-être vide ' +
        'ou la synchronisation Gmail n\'a pas encore tourné.</div></td></tr>';
      return;
    }
    tbody.innerHTML = data.items.map(function (row) {
      var subj = row.subject ? AM.escapeHtml(row.subject) : '<span class="muted">(sans objet)</span>';
      return '<tr>' +
        '<td class="mono">' + AM.formatDate(row.date_received) + '</td>' +
        '<td>' + AM.escapeHtml(row.sender || row.sender_email || '—') +
          '<br><small class="mono muted">' + AM.escapeHtml(row.sender_email || '') + '</small></td>' +
        '<td>' + subj + '</td>' +
        '<td>' + labelsBadges(row.labels) + '</td>' +
        '<td>' + stateBadges(row) + '</td>' +
        '<td><a class="btn btn--sm" href="/api/emails/' + encodeURIComponent(row.id) + '">détail JSON</a></td>' +
        '</tr>';
    }).join('');
  }

  function renderPager(data) {
    var info = AM.$('pager-info');
    var from = data.total === 0 ? 0 : state.offset + 1;
    var to = Math.min(state.offset + LIMIT, data.total);
    info.textContent = AM.formatNumber(from) + '–' + AM.formatNumber(to) +
      ' sur ' + AM.formatNumber(data.total);
    AM.$('prev').disabled = state.offset === 0;
    AM.$('next').disabled = state.offset + LIMIT >= data.total;
  }

  function load() {
    AM.showLoading('mails-body', 'Chargement des emails…');
    var qs = '?limit=' + LIMIT + '&offset=' + state.offset;
    if (state.sender) qs += '&sender=' + encodeURIComponent(state.sender);
    if (state.label) qs += '&label=' + encodeURIComponent(state.label);
    AM.fetchJSON('/api/emails' + qs).then(function (data) {
      renderRows(data);
      renderPager(data);
    }).catch(function (err) {
      AM.showError('mails-body', err, 'Impossible de charger les emails.');
      AM.$('pager-info').textContent = '—';
    });
  }

  function applyFilters() {
    state.sender = AM.$('f-sender').value.trim();
    state.label = AM.$('f-label').value.trim();
    state.offset = 0;
    pushUrl();
    load();
  }

  function resetFilters() {
    AM.$('f-sender').value = '';
    AM.$('f-label').value = '';
    state.sender = '';
    state.label = '';
    state.offset = 0;
    pushUrl();
    load();
  }

  AM.$('f-apply').addEventListener('click', applyFilters);
  AM.$('f-reset').addEventListener('click', resetFilters);
  AM.$('prev').addEventListener('click', function () {
    if (state.offset >= LIMIT) { state.offset -= LIMIT; pushUrl(); load(); }
  });
  AM.$('next').addEventListener('click', function () {
    state.offset += LIMIT; pushUrl(); load();
  });

  readFiltersFromUrl();
  load();
})();
