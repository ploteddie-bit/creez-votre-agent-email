/* learning.js — métriques d'apprentissage depuis GET /api/learning. */
(function () {
  'use strict';
  var AM = window.AM;

  var ACTION_LABELS = {
    archive: 'Archiver',
    mark_read: 'Marquer lu',
    star: 'Étoiler',
    move_ia_review: 'Déplacer (revue IA)'
  };

  function load() {
    AM.showLoading('precision-rows', 'Chargement…');
    AM.showLoading('domains-body', 'Chargement…');
    ['p2-enabled', 'p2-precision', 'p2-evaluated'].forEach(function (id) {
      AM.$(id).textContent = '—';
    });

    AM.fetchJSON('/api/learning').then(function (data) {
      if (data.window) AM.$('win-size').textContent = data.window;
      renderP2(data.progression_p2 || {});
      renderPrecision(data.window_stats || {});
      renderDomains(data.top_domains_appris || []);
    }).catch(function (err) {
      AM.showError('precision-rows', err, 'Impossible de charger les métriques d\'apprentissage.');
      AM.showError('domains-body', err, '');
    });
  }

  function renderP2(prog) {
    var on = prog.p2_enabled === true;
    AM.$('p2-enabled').innerHTML = on
      ? '<span class="badge badge--warn">activée</span>'
      : '<span class="badge">désactivée</span>';
    AM.$('p2-precision').textContent = AM.formatPct(prog.overall_precision_30d);
    var evaluated = (prog.approved_30d || 0) + (prog.rejected_30d || 0);
    AM.$('p2-evaluated').textContent = AM.formatNumber(evaluated);
  }

  function renderPrecision(windowStats) {
    var holder = AM.$('precision-rows');
    var actions = Object.keys(windowStats);
    if (actions.length === 0) {
      holder.innerHTML = '<div class="state">' +
        'Aucune opération n\'a encore de seuil configuré, ' +
        'ou aucune décision P2 n\'a été évaluée.</div>';
      return;
    }
    holder.innerHTML = actions.map(function (action) {
      var s = windowStats[action];
      var label = ACTION_LABELS[action] || action;
      var precisionPct = Math.round((s.precision || 0) * 100);
      var thresholdPct = Math.round((s.threshold || 0) * 100);
      var fillWidth = Math.min(100, precisionPct);
      var thresholdLeft = Math.max(0, Math.min(100, thresholdPct));
      var okClass = s.above_threshold ? 'badge--ok' : 'badge--warn';
      var statusTxt = s.above_threshold ? 'au-dessus du seuil' : 'sous le seuil';
      return '<div class="pbar-row" style="margin-bottom:14px">' +
        '<div><strong>' + AM.escapeHtml(label) + '</strong>' +
          '<br><small class="muted">' + AM.formatNumber(s.approved) + ' approuvées · ' +
          AM.formatNumber(s.rejected) + ' rejetées · ' +
          AM.formatNumber(s.pending) + ' en attente</small></div>' +
        '<div><div class="pbar"><div class="pbar__fill" style="width:' + fillWidth + '%"></div>' +
          '<div class="pbar__threshold" style="left:' + thresholdLeft + '%"></div></div></div>' +
        '<div class="mono" style="text-align:right">' +
          '<span class="badge ' + okClass + '">' + precisionPct + '%</span>' +
          '<br><small class="muted">seuil ' + thresholdPct + '%</small>' +
          '<br><small class="muted">' + statusTxt + '</small></div>' +
        '</div>';
    }).join('');
  }

  function renderDomains(domains) {
    var body = AM.$('domains-body');
    if (!domains.length) {
      body.innerHTML = '<tr><td colspan="2"><div class="state">' +
        'Aucun domaine n\'a encore d\'email archivé. ' +
        'L\'apprentissage se construit au fil des actions approuvées.</div></td></tr>';
      return;
    }
    body.innerHTML = domains.map(function (d) {
      return '<tr><td class="mono">' + AM.escapeHtml(d.domain || '—') + '</td>' +
        '<td class="num mono">' + AM.formatNumber(d.archived_count) + '</td></tr>';
    }).join('');
  }

  load();
})();
