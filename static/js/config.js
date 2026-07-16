/* config.js — lecture via GET /api/config, écriture via PUT /api/config. */
(function () {
  'use strict';
  var AM = window.AM;

  function render(cfg) {
    AM.$('k-env').textContent = cfg.environment || '—';
    AM.$('k-llm').textContent = cfg.ollama_llm_model || '—';
    AM.$('k-embed').textContent = cfg.ollama_embedding_model || '—';
    AM.$('k-poll').textContent =
      (cfg.polling_interval_seconds != null ? cfg.polling_interval_seconds + ' s' : '—');
    AM.$('k-max').textContent =
      (cfg.p2_max_daily_actions != null ? cfg.p2_max_daily_actions + ' /jour' : '—');
    AM.$('opt-p2').checked = cfg.p2_enabled === true;
    AM.$('opt-vacation').checked = cfg.vacation_mode === true;
  }

  function load() {
    AM.fetchJSON('/api/config').then(render).catch(function (err) {
      AM.toast('Lecture config impossible : ' + (err.message || err), 'err');
    });
  }

  function save() {
    var body = {
      p2_enabled: AM.$('opt-p2').checked,
      vacation_mode: AM.$('opt-vacation').checked,
    };
    var btn = AM.$('save');
    if (body.p2_enabled && !confirm(
      'Activer P2 autorise l\'agent à exécuter des actions Gmail de façon autonome.\n\n' +
      'Confirmer ?'
    )) {
      AM.$('opt-p2').checked = false;
      body.p2_enabled = false;
      return;
    }
    btn.disabled = true;
    btn.textContent = 'Enregistrement…';
    AM.fetchJSON('/api/config', { method: 'PUT', body: body }).then(function (cfg) {
      render(cfg);
      AM.toast('Configuration enregistrée (en mémoire).', 'ok');
    }).catch(function (err) {
      AM.toast('Échec : ' + (err.message || err), 'err');
    }).finally(function () {
      btn.disabled = false;
      btn.textContent = 'Enregistrer';
    });
  }

  AM.$('save').addEventListener('click', save);
  AM.$('reload').addEventListener('click', load);

  load();
})();
