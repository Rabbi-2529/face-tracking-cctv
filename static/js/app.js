/*
 * Page-wide behaviour: the sidebar, and the three widget libraries applied
 * to plain HTML so templates stay simple.
 *
 *   table.grid            -> DataTables (sort, filter, paging)
 *   select                -> Select2 (searchable, themed)
 *   date / datetime / time inputs -> flatpickr calendar
 *
 * Opt-outs, set on the element:
 *   data-dt="off"          no DataTables on this table
 *   data-dt-paging="off"   the server already paginates; filter and sort only
 *   data-no-select2        keep the native select
 *   data-no-picker         keep the native date/time input
 */
(function ($) {
  'use strict';

  // ---------------------------------------------------------------- shell

  function markActiveNav() {
    // Longest matching link wins, so /review/unknown/ highlights Review,
    // and the dashboard ("/") only highlights on the dashboard itself.
    var path = location.pathname, best = null, bestLen = -1;
    document.querySelectorAll('.sidebar a.nav-link').forEach(function (link) {
      // data-match widens a link to a whole section, e.g. all of /review/.
      var href = link.dataset.match || link.getAttribute('href');
      var match = href === '/' ? path === '/' : path.indexOf(href) === 0;
      if (match && href.length > bestLen) { best = link; bestLen = href.length; }
    });
    if (best) {
      best.classList.add('active');
      var crumb = document.getElementById('crumb-page');
      if (crumb) { crumb.textContent = best.textContent.trim(); }
    }
  }

  function wireMenu() {
    var toggle = document.querySelector('.menu-toggle');
    var scrim = document.querySelector('.scrim');
    if (toggle) { toggle.addEventListener('click', function () { document.body.classList.toggle('nav-open'); }); }
    if (scrim) { scrim.addEventListener('click', function () { document.body.classList.remove('nav-open'); }); }
  }

  // ----------------------------------------------------------- DataTables

  function initTables(root) {
    if (!$.fn.DataTable) { return; }
    $(root).find('table.grid').each(function () {
      var table = this;
      if (table.dataset.dt === 'off' || $.fn.DataTable.isDataTable(table) || !table.tHead) { return; }

      var body = table.tBodies[0];
      var columns = table.tHead.rows[0] ? table.tHead.rows[0].cells.length : 0;
      if (!body || !columns) { return; }

      // An "{% empty %}" row spans every column. DataTables cannot index it,
      // so its message becomes the table's own empty message instead.
      var emptyText = null;
      var rows = Array.prototype.slice.call(body.rows);
      if (rows.length === 1 && rows[0].cells.length === 1 && rows[0].cells[0].colSpan > 1) {
        emptyText = rows[0].textContent.trim();
        body.removeChild(rows[0]);
        rows = [];
      }
      // Any other spanning row would break column alignment: leave it alone.
      if (rows.some(function (r) { return r.cells.length !== columns; })) { return; }

      var serverPaged = table.dataset.dtPaging === 'off';
      var paging = !serverPaged && rows.length > 15;
      $(table).DataTable({
        paging: paging,
        pageLength: 15,
        lengthMenu: [15, 25, 50, 100],
        info: paging,
        searching: rows.length > 5,
        order: [],               // keep the server's order until a header is clicked
        autoWidth: false,
        layout: {
          topStart: paging ? 'pageLength' : null,
          topEnd: rows.length > 5 ? 'search' : null,
          bottomStart: paging ? 'info' : null,
          bottomEnd: paging ? 'paging' : null
        },
        language: {
          search: '',
          searchPlaceholder: serverPaged ? 'Filter this page...' : 'Search...',
          lengthMenu: '_MENU_ per page',
          info: '_START_-_END_ of _TOTAL_',
          infoEmpty: 'Nothing to show',
          zeroRecords: 'No matching rows',
          emptyTable: emptyText || 'Nothing to show',
          paginate: { previous: '&lsaquo;', next: '&rsaquo;' }
        }
      });
    });
  }

  // -------------------------------------------------------------- Select2

  function initSelects(root) {
    if (!$.fn.select2) { return; }
    $(root).find('select').each(function () {
      var el = this;
      if (el.hasAttribute('data-no-select2') || $(el).hasClass('select2-hidden-accessible')) { return; }
      if (el.closest('.dt-container') && el.classList.contains('dt-input')) { return; }
      var inline = !el.closest('form.stacked');
      $(el).select2({
        width: inline ? 'style' : '100%',
        dropdownAutoWidth: true,
        // A search box only where there is something to search.
        minimumResultsForSearch: el.options.length > 8 ? 0 : Infinity,
        placeholder: el.multiple ? 'Choose...' : undefined,
        closeOnSelect: !el.multiple
      });
    });
  }

  // ------------------------------------------------------------ flatpickr

  function initPickers(root) {
    if (typeof flatpickr === 'undefined') { return; }
    var base = { disableMobile: true, allowInput: true, monthSelectorType: 'static' };
    root.querySelectorAll('input[type="date"], input[type="datetime-local"], input[type="time"]').forEach(function (input) {
      if (input.hasAttribute('data-no-picker') || input._flatpickr) { return; }
      var kind = input.type, options;
      if (kind === 'date') {
        options = { dateFormat: 'Y-m-d', altInput: true, altFormat: 'j M Y' };
      } else if (kind === 'datetime-local') {
        options = { enableTime: true, time_24hr: true, dateFormat: 'Y-m-d\\TH:i', altInput: true, altFormat: 'j M Y, H:i' };
      } else {
        options = { enableTime: true, noCalendar: true, time_24hr: true, dateFormat: 'H:i' };
      }
      // The browser's own picker would fight flatpickr's, so the input
      // becomes text; the submitted value keeps the original format.
      input.type = 'text';
      flatpickr(input, Object.assign({}, base, options));
    });
  }

  // ---------------------------------------------------------------- start

  $(function () {
    markActiveNav();
    wireMenu();
    initPickers(document);
    initSelects(document);
    initTables(document);
  });

  window.cctvUI = { initTables: initTables, initSelects: initSelects, initPickers: initPickers };
})(jQuery);
