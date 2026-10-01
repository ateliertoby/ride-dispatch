// The order's own sheet: its details, the fields that can be edited, and the
// numpad that edits them.  The day view and the settle view both open it, so
// an order reads and edits the same wherever it is found.
//
// The view opening it supplies a host through useOrderHost():
//   order()        the order the sheet shows, or null once it is not there
//   push(view)     stack a view: a function that fills the sheet element
//   patch(body)    PATCH the order; resolve false when it failed, which keeps
//                  the numpad up for a retry
//   cancelled(id)  the order was cancelled: leave its sheet and refresh
//   gone()         the order is no longer there to show
//   head(title, sub)  the markup of a view's heading, with the view's own
//                  close (and back) controls; title and sub are HTML
//   pop()          close the top view the way that view closes any other
//   rowsBefore(o)  optional: the view's own info rows above the order's
//   rowsAfter(o)   the view's own info rows below them
//   subtitle(o)    optional: the line under the title
// numpadView draws its heading through the host too, so a view that uses the
// numpad outside an order's sheet must have set its host first.
//
// The markup this file writes carries inline handlers, which resolve names on
// window and cannot reach module scope, so the functions they call are
// published under window.rd.sheet.

import { $, apiWrite, collectContactLines, esc, expectedOf, isFlightPickup, money,
         orderTime, platform, shortId, svcLabel, tight, toast } from './shared.js';
import { AuthExpired } from './api.js';

let host = null;
// Both views stay mounted, so "the page's host" is whichever view is showing;
// that view sets it when it is shown and before it opens an order.
export function useOrderHost(h) { host = h; }

// A figure in the mono face. Takes text, not markup.
const num = text => '<span class="num">' + tight(esc(text)) + '</span>';

// A batch's expected total was frozen from these fields, and cancelling would
// take the order out of that total, so the server refuses both on a batched
// order (db.BATCH_LOCKED_FIELDS).  The sheet says so before the tap.
const BATCH_LOCKED = new Set(['price', 'tunnel_fee', 'banner_fee']);
const BATCH_LOCKED_MSG = '已結算嘅單要先撤銷結算';
function isBatched(o) { return o.settlement_id != null; }

function editableFields(o) {
  const p = platform(o);
  if (p === 'didi') return [['price', '車費'], ['tunnel_fee', '隧道費'], ['time', '時間']];
  if (p === 'uber') return [['price', '總收入'], ['tunnel_fee', '通行費'], ['time', '時間']];
  if (p === 'foodpanda') return [['price', '價錢'], ['time', '時間']];
  return [['price', '價錢'], ['tunnel_fee', '隧道費'], ['parking_fee', '停車費'], ['banner_fee', '舉牌費'], ['time', '時間']];
}

export function detailView(sheet) {
  const o = host.order();
  if (!o) { host.gone(); return; }
  const p = platform(o);
  const isPickup = isFlightPickup(o.service_type);
  const label = svcLabel(o.service_type);
  const locked = isBatched(o);

  const rows = host.rowsBefore ? host.rowsBefore(o) : [];
  if (p === 'ride') {
    if (o.passenger_name) rows.push(['乘客', esc(o.passenger_name)]);
    for (const [cl, cv] of collectContactLines(o)) {
      rows.push([esc(cl), '<a class="num" href="tel:' + esc(cv.replace(/\s+/g, '')) + '">' + esc(cv) + '</a>']);
    }
    if (o.flight_number) {
      let f = num(o.flight_number);
      if (o.flight_status === 'gate' && o.flight_gate) f += ' · 已到閘 ' + num(o.flight_gate);
      else if (o.flight_status === 'landed' && o.flight_eta) f += ' · 已降落 ' + num(o.flight_eta);
      else if (o.flight_eta) f += ' · 預計 ' + num(o.flight_eta);
      else if (o.flight_scheduled) f += ' · 預定 ' + num(o.flight_scheduled);
      rows.push(['航班', f]);
    }
    if (o.vehicle_type) rows.push(['車型', esc(o.vehicle_type)]);
    if (o.pickup) rows.push(['路線', esc(o.pickup) + '<br><span class="arrow">&rarr;</span> ' + esc(o.dropoff)]);
    if (o.driver_notes) rows.push(['備註', esc(o.driver_notes)]);
  }
  // What the platform took back, and what the trip is worth after it.  The row
  // above shows the gross, so without this the sheet would not explain why the
  // settlement figure is smaller.
  if (o.penalty_fee > 0) {
    rows.push(['判罰', '<span class="fine">' + num(money(-o.penalty_fee)) + '</span>（淨收 ' + num(money(expectedOf(o))) + '）']);
  }
  rows.push(...host.rowsAfter(o));
  // The net figure is the sheet's bottom line wherever a view states one.
  const info = '<div class="info">' + rows.map(r =>
    '<div class="info-row' + (r[0] === '淨收' ? ' sum' : '') + '"><span class="k">' + r[0] +
    '</span><span class="v">' + r[1] + '</span></div>'
  ).join('') + '</div>';

  // Where a flight pickup meets the passenger. Choosing one also sets that
  // place's first-hour parking charge (server-side); a car park visit linked to
  // the order later overwrites both with what HKIA saw.
  let pointRow = '';
  if (p === 'ride' && isPickup) {
    const cur = o.pickup_point || '';
    const opts = Object.entries(PICKUP_POINTS).map(([pt, fee]) =>
      '<button class="pp-opt' + (pt === cur ? ' on' : '') + '" onclick="rd.sheet.setPickupPoint(\'' + pt + '\')">' +
      '<span class="pp-n">' + pt + '</span><small>' + num(money(fee)) + '</small></button>');
    // A car park HKIA reported that is not one of ours shows as it was named.
    if (cur && !(cur in PICKUP_POINTS)) opts.push('<span class="pp-opt on"><span class="pp-n">' + esc(cur) + '</span></span>');
    pointRow = '<div class="pp-row"><span class="fk">上車點</span><div class="pp-seg">' + opts.join('') + '</div></div>';
  }

  const fields = editableFields(o).map(([key, fLabel]) => {
    let value, unset = false;
    if (key === 'time') {
      value = orderTime(o);
    } else {
      const v = o[key];
      if (key === 'price' && !v) { value = '未入價'; unset = true; }
      else value = '$' + $(v || 0);
    }
    const frozen = locked && BATCH_LOCKED.has(key);
    const cells = '<span class="fk">' + fLabel + '</span>' +
      '<span class="fv' + (unset ? ' unset' : '') + '">' + (unset ? esc(value) : tight(esc(value))) + '</span>';
    // A frozen field is text, not a control: it reads as a figure with the
    // reason beside its label. A tap on it still says what to do about it.
    const row = frozen
      ? '<div class="field-row locked" onclick="rd.sheet.editField(\'' + key + '\')">' + cells +
        '<span class="chev">已結算</span></div>'
      : '<button class="field-row" onclick="rd.sheet.editField(\'' + key + '\')">' + cells +
        '<span class="chev">&rsaquo;</span></button>';
    // Waiving the parking fee is the everyday action (the pickup default often
    // turns out not to be charged), so it gets a one-tap pill; any other
    // amount is rare and goes through the numpad as usual.
    const lead = key === 'parking_fee' ? pointRow : '';
    if (key === 'parking_fee' && o.parking_fee > 0) {
      return lead + '<div class="fr-split">' + row +
        '<button class="fr-waive" onclick="rd.sheet.waiveParking()">免</button></div>';
    }
    return lead + row;
  }).join('');

  // The heading leads with what the board's code column shows for the order:
  // a flight pickup's flight number, any other order's service.
  const code = isPickup && o.flight_number ? num(o.flight_number) : esc(label);
  sheet.insertAdjacentHTML('beforeend',
    host.head('<span class="hd-code">' + code + '</span> ' + num(orderTime(o)),
      host.subtitle ? host.subtitle(o) : num('#' + shortId(o.order_id))) +
    info + fields +
    (p === 'ride' ? '<a class="tg-link" href="https://t.me/agent_ride_bot?start=order_' + encodeURIComponent(o.order_id) + '">喺 Telegram 開</a>' : '') +
    (locked
      ? '<div class="cancel-note">' + BATCH_LOCKED_MSG + '先取消得</div>'
      : '<button class="cancel-link" onclick="rd.sheet.openCancelConfirm()">取消訂單</button>')
  );
}

function editField(key) {
  const o = host.order();
  if (!o) return;
  if (isBatched(o) && BATCH_LOCKED.has(key)) { toast(BATCH_LOCKED_MSG); return; }
  const fLabel = editableFields(o).find(f => f[0] === key)[1];
  if (key === 'time') {
    host.push(numpadView({
      title: '改時間', hint: '而家 ' + orderTime(o), mode: 'time',
      onConfirm: v => host.patch({ time: v }),
    }));
  } else {
    const quick = key === 'parking_fee' && o.parking_fee > 0
      ? { label: '免停車費', onTap: () => host.patch({ parking_fee: 0 }) }
      : null;
    host.push(numpadView({
      title: '改' + fLabel, hint: '而家 $' + $(o[key] || 0), mode: 'money', quick,
      onConfirm: v => host.patch({ [key]: v }),
    }));
  }
}

function waiveParking() { host.patch({ parking_fee: 0 }); }

// JS twin of ingest.py:PICKUP_POINTS -- keep in sync. Each place and its
// first-hour charge, which the server writes when the place is chosen; the
// sheet shows it under the name so the choice is made knowing its cost.
const PICKUP_POINTS = { 'P1': 35, 'P4': 32, '富豪': 0 };

function setPickupPoint(pt) {
  const o = host.order();
  if (!o || o.pickup_point === pt) return;
  host.patch({ pickup_point: pt });
}

// Cancellation confirms in its own pushed view rather than an armed state on
// the same button: a page reload redraws the order's sheet, so a stacked view
// survives the SSE-driven re-renders that would wipe any in-place armed state
// mid-decision, and no disarm timer is needed at all.
function openCancelConfirm() { host.push(cancelConfirmView); }

function cancelConfirmView(sheet) {
  const o = host.order();
  if (!o) { host.gone(); return; }
  const lines = [
    num(orderTime(o)) + (o.passenger_name ? ' · ' + esc(o.passenger_name) : ''),
    o.pickup ? esc(o.pickup + ' → ' + o.dropoff) : '',
  ].filter(Boolean).join('<br>');
  sheet.insertAdjacentHTML('beforeend',
    host.head('取消訂單', num('#' + shortId(o.order_id))) +
    '<div class="cancel-info">' + lines + '</div>' +
    '<button class="primary-btn danger" id="cancelGo">確認取消</button>' +
    '<button class="ghost-btn" onclick="rd.sheet.pop()">返回</button>'
  );
  sheet.querySelector('#cancelGo').addEventListener('click', async function () {
    // Read at the tap: by the time the server answers, the operator can have
    // switched to the other view, and the host is then that view's.
    const asked = host;
    this.disabled = true;
    this.textContent = '取消緊…';
    const id = o.order_id;
    try {
      await apiWrite('PATCH', '/api/orders/' + encodeURIComponent(id), { status: 'cancelled' });
    } catch (e) {
      if (!(e instanceof AuthExpired)) toast(e.message);
      this.disabled = false;
      this.textContent = '確認取消';
      return;
    }
    asked.cancelled(id);
  });
}

// ---- numpad ----
// mode: 'money' (digits + dot) | 'time' (HHMM)
// onConfirm may be async; returning false means "stay here and reset for retry"
// One element order for every caller: display → confirm → gap → keypad, the
// keypad bottommost (see the layout-rule comment in the CSS).
// layout 'pay' (paste pricing): the host becomes a flex column (.np-sheet)
// with the parsed preview on top and a links row (manual-entry toggle, skip).
// suggest: prefill the amount, flank it with −/+$10 steppers, and keep the
// keypad collapsed behind the 自己入價 toggle; the first typed key replaces
// the prefill wholesale (typing means "I'm entering my own number").
// skip: secondary action in the links row; its handler may return false to
// re-enable the button for retry.
export function numpadView({ title, sub, hint, mode, onConfirm, quick, above, layout, suggest, skip }) {
  let input = suggest != null ? String(suggest) : '';
  let pristine = suggest != null;
  const pay = layout === 'pay';
  function view(sheet) {
    const suggested = pay && suggest != null;
    if (pay) sheet.classList.add('np-sheet');
    const padHtml =
      '<div class="np-pad' + (suggested ? '' : ' open') + '" id="npPad"><div>' +
      '<div class="numpad" id="npKeys"></div></div></div>';
    sheet.insertAdjacentHTML('beforeend',
      host.head(esc(title), sub || '') +
      (above || '') +
      (pay
        ? '<div class="np-links">' +
            (suggested ? '<span class="np-links-opt" id="npOpt"><button id="npPadToggle">自己入價</button><span>·</span></span>' : '') +
            (skip ? '<button id="npSkip">' + esc(skip.label) + '</button>' : '') +
          '</div>' +
          '<div class="np-amount-row' + (suggested ? '' : ' expanded') + '" id="npRow">' +
            (suggested ? '<button class="step" data-s="-10">&minus;$10</button>' : '') +
            '<div class="numpad-display" id="npDisplay"></div>' +
            (suggested ? '<button class="step" data-s="10">+$10</button>' : '') +
          '</div>' +
          '<button class="primary-btn np-ok" id="npOk" disabled>確認</button>' +
          padHtml
        : '<div class="numpad-display" id="npDisplay"></div>' +
          '<div class="numpad-hint">' + tight(esc(hint || '')) + '</div>' +
          (quick ? '<button class="ghost-btn" id="npQuick">' + esc(quick.label) + '</button>' : '') +
          '<button class="primary-btn" id="npOk" disabled>確認</button>' +
          padHtml)
    );
    const keys = mode === 'money'
      ? ['1','2','3','4','5','6','7','8','9','.','0','⌫']
      : ['1','2','3','4','5','6','7','8','9','','0','⌫'];
    // Lookups must be scoped to the host: both the drop panel and the bottom
    // sheet can hold a numpad's markup at the same time (one of them hidden),
    // so document-wide ids would resolve to the wrong copy.
    const keysEl = sheet.querySelector('#npKeys');
    keysEl.innerHTML = keys.map(k =>
      k === '' ? '<div></div>' :
      '<button class="key' + (k === '⌫' || k === '.' ? ' fn' : '') + '" data-k="' + k + '">' + k + '</button>'
    ).join('');

    const display = sheet.querySelector('#npDisplay');
    const ok = sheet.querySelector('#npOk');

    function valid() {
      if (mode === 'money') return input !== '' && input !== '.' && !isNaN(parseFloat(input));
      if (input.length !== 4) return false;
      return +input.slice(0, 2) <= 23 && +input.slice(2) <= 59;
    }
    function paint() {
      if (mode === 'money') {
        display.innerHTML = (input ? tight('$' + esc(input)) : '<span class="dim">$0</span>') +
          (suggest != null && input === String(suggest) ? '<span class="sug-tag">建議</span>' : '');
      } else {
        const dim = '<span class="dim">&#8211;</span>';
        const digit = i => i < input.length ? esc(input[i]) : dim;
        display.innerHTML = digit(0) + digit(1) + tight(':') + digit(2) + digit(3);
      }
      ok.disabled = !valid();
    }
    keysEl.addEventListener('click', e => {
      const k = e.target.dataset && e.target.dataset.k;
      if (!k) return;
      if (pristine) { input = ''; pristine = false; }  // typing over a prefill starts fresh (⌫ clears it outright)
      if (k === '⌫') input = input.slice(0, -1);
      else if (k === '.') { if (!input.includes('.')) input += input ? '.' : '0.'; }
      else {
        if (mode === 'time' && input.length >= 4) return;
        if (mode === 'money' && input.length >= 7) return;
        input += k;
      }
      paint();
    });
    ok.addEventListener('click', async () => {
      if (!valid()) return;
      ok.disabled = true;
      const value = mode === 'money' ? parseFloat(input)
        : input.slice(0, 2) + ':' + input.slice(2);
      const result = await onConfirm(value);
      if (result === false) {
        // retry keeps a usable state: back to the suggestion when there is
        // one, since the keypad may still be tucked away
        input = suggest != null ? String(suggest) : '';
        pristine = suggest != null;
        paint();
      }
    });
    const rowEl = sheet.querySelector('#npRow');
    if (rowEl) rowEl.addEventListener('click', e => {
      const s = e.target.dataset && e.target.dataset.s;
      if (!s) return;
      input = String(Math.max(0, (parseFloat(input) || 0) + Number(s)));
      paint();
    });
    const padToggle = sheet.querySelector('#npPadToggle');
    if (padToggle) padToggle.addEventListener('click', () => {
      rowEl.classList.add('expanded');
      sheet.querySelector('#npPad').classList.add('open');
      sheet.querySelector('#npOpt').classList.add('gone');
    });
    const skipEl = sheet.querySelector('#npSkip');
    if (skip && skipEl) skipEl.addEventListener('click', async function () {
      this.disabled = true;
      if (await skip.onTap() === false) this.disabled = false;
    });
    const quickEl = sheet.querySelector('#npQuick');
    if (quick && quickEl) quickEl.addEventListener('click', quick.onTap);
    paint();
  }
  return view;
}

window.rd = window.rd || {};
window.rd.sheet = { editField, waiveParking, setPickupPoint, openCancelConfirm,
                    pop: () => host.pop() };
