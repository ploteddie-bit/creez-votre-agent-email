/* stats.js — agrégats depuis GET /api/stats?days=N. */
(function () {
  'use strict';
  var AM = window.AM;

  function load() {
    var days = AM.$('days').value;
    // State loading
    ['actions-body', 'senders-body', 'daily-body'].forEach(function (id) {
      AM.showLoading(id, 'Chargement…');
    });
    ['c-emails', 'c-decisions', 'c-actions'].forEach(function (id) {
      AM.$(id).textContent = '—';
    });

    AM.fetchJSON('/api/stats?days=' + encodeURIComponent(days)).then(function (data) {
      renderCounters(data.counters);
      renderActions(data.repartition_actions);
      renderSenders(data.top_senders);
      renderDaily(data.actions_par_jour);
    }).catch(function (err) {
      var msg = 'Impossible de charger les statistiques.';
      ['actions-body', 'senders-body', 'daily-body'].forEach(function (id) {
        AM.showError(id, err, msg);
      });
    });
  }

  function renderCounters(c) {
    AM.$('c-emails').textContent = AM.formatNumber(c.total_emails);
    AM.$('c-decisions').textContent = AM.formatNumber(c.total_decisions);
    AM.$('c-actions').textContent = AM.formatNumber(c.total_actions_done);
  }

  function renderActions(rep) {
    var body = AM.$('actions-body');
    var entries = Object.keys(rep || {}).map(function (k) {
      return { op: k, count: rep[k] };
    }).sort(function (a, b) { return b.count - a.count; });

    if (entries.length === 0) {
      body.innerHTML = '<tr><td colspan="3"><div class="state">' +
        'Aucune décision sur la période. Le journal est vide ou le daemon ' +
        'Recommender n\'a pas tourné récemment.</div></td></tr>';
      return;
    }
    var total = entries.reduce(function (s, e) { return s + e.count; }, 0) || 1;
    body.innerHTML = entries.map(function (e) {
      var pct = Math.round((e.count / total) * 100);
      return '<tr><td>' + AM.operationBadge(e.op) + '</td>' +
        '<td class="num mono">' + AM.formatNumber(e.count) + '</td>' +
        '<td class="num">' + pct + '%</td></tr>';
    }).join('');
  }

  function renderSenders(senders) {
    var body = AM.$('senders-body');
    if (!senders || senders.length === 0) {
      body.innerHTML = '<tr><td colspan="3"><div class="state">' +
        'Aucun email sur la période (base vide ou sync Gmail à lancer).</div></td></tr>';
      return;
    }
    body.innerHTML = senders.map(function (s) {
      return '<tr><td class="mono">' + AM.escapeHtml(s.sender_email || '—') + '</td>' +
        '<td class="mono muted">' + AM.escapeHtml(s.sender_domain || '—') + '</td>' +
        '<td class="num mono">' + AM.formatNumber(s.count) + '</td></tr>';
    }).join('');
  }

  function renderDaily(daily) {
    var body = AM.$('daily-body');
    if (!daily || daily.length === 0) {
      body.innerHTML = '<tr><td colspan="3"><div class="state">' +
        'Aucune activité décisionnelle sur la période.</div></td></tr>';
      return;
    }
    var max = Math.max.apply(null, daily.map(function (d) { return d.count; })) || 1;
    // On affiche du plus récent au plus ancien
    var rows = daily.slice().reverse();
    body.innerHTML = rows.map(function (d) {
      var w = Math.max(2, Math.round((d.count / max) * 100));
      return '<tr><td class="mono">' + AM.escapeHtml(d.day) + '</td>' +
        '<td class="num mono">' + AM.formatNumber(d.count) + '</td>' +
        '<td><div class="pbar"><div class="pbar__fill" style="width:' + w + '%"></div></div></td></tr>';
    }).join('');
  }

  AM.$('days').addEventListener('change', load);
  load();
})();
