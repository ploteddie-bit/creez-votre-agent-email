/* decisions.js — journal des décisions + approve/reject via API. */
(function () {
  'use strict';
  var AM = window.AM;
  var LIMIT = 25;
  var state = { offset: 0, phase: '', classification: '', approved: '' };

  function readFiltersFromUrl() {
    state.phase = AM.queryParam('phase') || '';
    state.classification = AM.queryParam('classification') || '';
    state.approved = AM.queryParam('approved') || '';
    var off = parseInt(AM.queryParam('offset'), 10);
    state.offset = isNaN(off) || off < 0 ? 0 : off;
    AM.$('f-phase').value = state.phase;
    AM.$('f-class').value = state.classification;
    AM.$('f-approved').value = state.approved;
  }

  function pushUrl() {
    var p = new URLSearchParams();
    if (state.phase) p.set('phase', state.phase);
    if (state.classification) p.set('classification', state.classification);
    if (state.approved) p.set('approved', state.approved);
    if (state.offset) p.set('offset', String(state.offset));
    var qs = p.toString();
    window.history.replaceState(null, '', '/decisions' + (qs ? '?' + qs : ''));
  }

  function confidenceCell(row) {
    var c = row.final_confidence != null ? row.final_confidence : row.llm_confidence;
    if (c == null) return '<span class="muted">—</span>';
    var pct = Math.round(Number(c) * 100);
    var cls = pct >= 80 ? 'badge--ok' : (pct >= 50 ? 'badge--warn' : 'badge--danger');
    return '<span class="badge ' + cls + '">' + pct + '%</span>';
  }

  function actionsCell(row) {
    // Approve/reject n'ont de sens que pour les décisions P1 en attente.
    if (row.phase !== 'P1') return '<span class="muted">—</span>';
    if (row.user_approved === true || row.user_approved === false) {
      return '<span class="muted">traité</span>';
    }
    return '<button class="btn btn--ok btn--sm" data-action="approve" data-id="' + row.id + '">Approuver</button> ' +
           '<button class="btn btn--danger btn--sm" data-action="reject" data-id="' + row.id + '">Rejeter</button>';
  }

  function renderRows(data) {
    var tbody = AM.$('dec-body');
    if (!data.items || data.items.length === 0) {
      tbody.innerHTML = '<tr><td colspan="8"><div class="state">' +
        'Aucune décision dans le journal. L\'agent n\'a pas encore analysé d\'email ' +
        '(le daemon Recommender n\'a peut-être pas tourné).</div></td></tr>';
      return;
    }
    tbody.innerHTML = data.items.map(function (row) {
      var rationale = row.rationale ? AM.escapeHtml(AM.truncate(row.rationale, 90)) : '<span class="muted">—</span>';
      return '<tr>' +
        '<td class="mono">' + AM.formatDate(row.created_at) + '</td>' +
        '<td>' + AM.phaseBadge(row.phase) + '</td>' +
        '<td>' + AM.escapeHtml(row.classification || '—') + '</td>' +
        '<td>' + AM.operationBadge(row.executable_operation) + '</td>' +
        '<td class="num">' + confidenceCell(row) + '</td>' +
        '<td>' + AM.approvalBadge(row.user_approved) + '</td>' +
        '<td>' + rationale + '</td>' +
        '<td>' + actionsCell(row) + '</td>' +
        '</tr>';
    }).join('');

    // Wire approve/reject
    tbody.querySelectorAll('[data-action]').forEach(function (btn) {
      btn.addEventListener('click', onAction);
    });
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
    AM.showLoading('dec-body', 'Chargement du journal…');
    var qs = '?limit=' + LIMIT + '&offset=' + state.offset;
    if (state.phase) qs += '&phase=' + encodeURIComponent(state.phase);
    if (state.classification) qs += '&classification=' + encodeURIComponent(state.classification);
    if (state.approved === 'true') qs += '&approved=true';
    else if (state.approved === 'false') qs += '&approved=false';
    AM.fetchJSON('/api/decisions' + qs).then(function (data) {
      renderRows(data);
      renderPager(data);
    }).catch(function (err) {
      AM.showError('dec-body', err, 'Impossible de charger les décisions.');
    });
  }

  function onAction(ev) {
    var btn = ev.currentTarget;
    var id = btn.getAttribute('data-id');
    var action = btn.getAttribute('data-action');
    if (!confirm((action === 'approve' ? 'Approuver' : 'Rejeter') + ' la décision #' + id + ' ?')) return;
    btn.disabled = true;
    AM.fetchJSON('/api/decisions/' + id + '/' + action, { method: 'POST' })
      .then(function () {
        AM.toast(action === 'approve' ? 'Décision #' + id + ' approuvée.' : 'Décision #' + id + ' rejetée.', 'ok');
        load();
      })
      .catch(function (err) {
        AM.toast('Échec : ' + (err.message || err), 'err');
        btn.disabled = false;
      });
  }

  function applyFilters() {
    state.phase = AM.$('f-phase').value;
    state.classification = AM.$('f-class').value;
    state.approved = AM.$('f-approved').value;
    state.offset = 0;
    pushUrl();
    load();
  }

  function resetFilters() {
    AM.$('f-phase').value = '';
    AM.$('f-class').value = '';
    AM.$('f-approved').value = '';
    state.phase = ''; state.classification = ''; state.approved = '';
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
