/* ============================================================
   Agent Mail 24/7 — Helpers JS partagés par les pages dashboard.
   Chargé avant les scripts spécifiques (mails.js, decisions.js...).
   CSP : script-src 'self' => tout le JS est dans des fichiers .js
   externes, jamais inline dans le HTML.
   ============================================================ */
(function (global) {
  'use strict';

  var AM = global.AM || (global.AM = {});

  // --- Réglages par défaut ---
  AM.apiBase = ''; // même origine que les pages

  // --- Utilitaires ---

  /** Échappe le HTML pour éviter les injections XSS côté rendu client. */
  function escapeHtml(value) {
    if (value === null || value === undefined) return '';
    return String(value)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }
  AM.escapeHtml = escapeHtml;

  /** Formate une date ISO en chaîne lisible FR (YYYY-MM-DD HH:MM). */
  function formatDate(iso) {
    if (!iso) return '—';
    try {
      var d = new Date(iso);
      if (isNaN(d.getTime())) return escapeHtml(iso);
      var pad = function (n) { return n < 10 ? '0' + n : String(n); };
      return d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate()) +
        ' ' + pad(d.getHours()) + ':' + pad(d.getMinutes());
    } catch (e) {
      return escapeHtml(iso);
    }
  }
  AM.formatDate = formatDate;

  /** Formate un nombre avec séparateurs de milliers. */
  function formatNumber(n) {
    if (n === null || n === undefined) return '0';
    return Number(n).toLocaleString('fr-FR');
  }
  AM.formatNumber = formatNumber;

  /** Formate un flottant en pourcentage (0.95 -> "95%"). */
  function formatPct(f) {
    if (f === null || f === undefined || isNaN(f)) return '—';
    return Math.round(Number(f) * 100) + '%';
  }
  AM.formatPct = formatPct;

  /** Tronque un texte à n caractères avec ellipsis. */
  function truncate(s, n) {
    n = n || 60;
    if (!s) return '';
    s = String(s);
    return s.length > n ? s.slice(0, n - 1) + '…' : s;
  }
  AM.truncate = truncate;

  /** fetch wrapper qui gère JSON + erreurs réseau. */
  function fetchJSON(path, options) {
    options = options || {};
    var headers = options.headers || {};
    if (options.body && typeof options.body !== 'string') {
      headers['Content-Type'] = 'application/json';
      options.body = JSON.stringify(options.body);
    } else if (options.body) {
      headers['Content-Type'] = headers['Content-Type'] || 'application/json';
    }
    options.headers = headers;
    return fetch(AM.apiBase + path, options).then(function (resp) {
      if (resp.status === 204) return null;
      // On tente le JSON même en erreur pour récupérer le détail
      return resp.json().then(
        function (data) {
          if (!resp.ok) {
            var err = new Error(
              (data && (data.detail || data.error)) ||
              ('HTTP ' + resp.status)
            );
            err.status = resp.status;
            err.body = data;
            throw err;
          }
          return data;
        },
        function () {
          // Corps non-JSON
          if (!resp.ok) {
            var e = new Error('HTTP ' + resp.status);
            e.status = resp.status;
            throw e;
          }
          return null;
        }
      );
    });
  }
  AM.fetchJSON = fetchJSON;

  /** Récupère le paramètre d'URL (?key=value). */
  function queryParam(key) {
    var params = new URLSearchParams(global.location.search);
    return params.get(key);
  }
  AM.queryParam = queryParam;

  /** Raccourci pour document.getElementById. */
  function $(id) { return document.getElementById(id); }
  AM.$ = $;

  // --- Rendu des états standard ---

  /** Affiche un état de chargement dans un élément cible. */
  function showLoading(el, msg) {
    if (typeof el === 'string') el = $(el);
    if (!el) return;
    el.innerHTML = '<div class="state"><span class="spinner"></span>' +
      escapeHtml(msg || 'Chargement…') + '</div>';
  }
  AM.showLoading = showLoading;

  /** Affiche un état vide dans un élément cible. */
  function showEmpty(el, msg) {
    if (typeof el === 'string') el = $(el);
    if (!el) return;
    el.innerHTML = '<div class="state">' + escapeHtml(msg || 'Aucune donnée.') + '</div>';
  }
  AM.showEmpty = showEmpty;

  /** Affiche une erreur dans un élément cible. */
  function showError(el, err, msg) {
    if (typeof el === 'string') el = $(el);
    if (!el) return;
    var detail = (err && err.message) ? err.message : '';
    el.innerHTML = '<div class="state state--error">' +
      escapeHtml(msg || 'Erreur lors du chargement.') +
      (detail ? '<br><small>' + escapeHtml(detail) + '</small></div>' : '</div>');
  }
  AM.showError = showError;

  /** Toast temporaire (succès ou erreur). */
  function toast(msg, kind) {
    var t = document.createElement('div');
    t.className = 'toast' + (kind ? ' toast--' + kind : '');
    t.textContent = msg;
    document.body.appendChild(t);
    setTimeout(function () {
      if (t.parentNode) t.parentNode.removeChild(t);
    }, 3500);
  }
  AM.toast = toast;

  // --- Badge helpers ---

  /** Badge coloré selon la valeur (true/false/null). */
  function approvalBadge(v) {
    if (v === true) return '<span class="badge badge--ok">approuvé</span>';
    if (v === false) return '<span class="badge badge--danger">rejeté</span>';
    return '<span class="badge">en attente</span>';
  }
  AM.approvalBadge = approvalBadge;

  /** Badge pour une opération exécutable. */
  function operationBadge(op) {
    if (!op || op === 'none') return '<span class="badge">aucune</span>';
    return '<span class="badge badge--accent">' + escapeHtml(op) + '</span>';
  }
  AM.operationBadge = operationBadge;

  /** Badge pour une phase P0/P1/P2. */
  function phaseBadge(phase) {
    var cls = 'badge';
    if (phase === 'P2') cls = 'badge badge--warn';
    else if (phase === 'P1') cls = 'badge badge--accent';
    return '<span class="' + cls + '">' + escapeHtml(phase || '?') + '</span>';
  }
  AM.phaseBadge = phaseBadge;

  // --- Nav active ---
  /** Marque le lien de navigation courant comme actif. */
  function markActiveNav() {
    var path = global.location.pathname.replace(/\/$/, '');
    var links = document.querySelectorAll('.xnav a[data-route]');
    links.forEach(function (a) {
      var route = a.getAttribute('data-route');
      if (route === path || (route !== '/' && path.indexOf(route) === 0)) {
        a.classList.add('active');
      }
    });
  }
  document.addEventListener('DOMContentLoaded', markActiveNav);
})(typeof window !== 'undefined' ? window : this);
