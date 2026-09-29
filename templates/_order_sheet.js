// The order's own sheet: its details, the fields that can be edited, and the
// numpad that edits them.  The dashboard and the settle page both open it, so
// an order reads and edits the same wherever it is found.  Included inside
// each page's <script> after _shared.js.
//
// The page opening it supplies `orderHost`:
//   order()        the order the sheet shows, or null once it is not there
//   push(view)     stack a view: a function that fills the sheet element
//   patch(body)    PATCH the order; resolve false when it failed, which keeps
//                  the numpad up for a retry
//   cancelled(id)  the order was cancelled: leave its sheet and refresh
//   gone()         the order is no longer there to show
//   rowsBefore(o)  optional: the page's own info rows above the order's
//   rowsAfter(o)   the page's own info rows below them
//   subtitle(o)    optional: the line under the title
// Views close through the page's own sheetHead and popView, the way that page
// closes any other view.

function editableFields(o) {
  const p = platform(o);
  if (p === 'didi') return [['price', '車費'], ['tunnel_fee', '隧道費'], ['time', '時間']];
  if (p === 'uber') return [['price', '總收入'], ['tunnel_fee', '通行費'], ['time', '時間']];
  if (p === 'foodpanda') return [['price', '價錢'], ['time', '時間']];
  return [['price', '價錢'], ['tunnel_fee', '隧道費'], ['parking_fee', '停車費'], ['banner_fee', '舉牌費'], ['time', '時間']];
}

function detailView(sheet) {
  const o = orderHost.order();
  if (!o) { orderHost.gone(); return; }
  const p = platform(o);
  const isPickup = isFlightPickup(o.service_type);
  const label = svcLabel(o.service_type);

  const rows = orderHost.rowsBefore ? orderHost.rowsBefore(o) : [];
  if (p === 'ride') {
    if (o.passenger_name) rows.push(['乘客', esc(o.passenger_name)]);
    for (const [cl, cv] of collectContactLines(o)) {
      rows.push([esc(cl), '<a href="tel:' + esc(cv.replace(/\s+/g, '')) + '">' + esc(cv) + '</a>']);
    }
    if (o.flight_number) {
      let f = esc(o.flight_number);
      if (o.flight_status === 'gate' && o.flight_gate) f += ' · 已到閘 ' + esc(o.flight_gate);
      else if (o.flight_status === 'landed' && o.flight_eta) f += ' · 已降落 ' + esc(o.flight_eta);
      else if (o.flight_eta) f += ' · 預計 ' + esc(o.flight_eta);
      else if (o.flight_scheduled) f += ' · 預定 ' + esc(o.flight_scheduled);
      rows.push(['航班', f]);
    }
    if (o.vehicle_type) rows.push(['車型', esc(o.vehicle_type)]);
    if (o.pickup) rows.push(['路線', esc(o.pickup) + '<br><span style="color:var(--text-3)">&rarr;</span> ' + esc(o.dropoff)]);
    if (o.driver_notes) rows.push(['備註', esc(o.driver_notes)]);
  }
  // What the platform took back, and what the trip is worth after it.  The row
  // above shows the gross, so without this the sheet would not explain why the
  // settlement figure is smaller.
  if (o.penalty_fee > 0) {
    rows.push(['判罰', esc(money(-o.penalty_fee) + '（淨收 ' + money(expectedOf(o)) + '）')]);
  }
  rows.push(...orderHost.rowsAfter(o));
  const info = '<div class="info">' + rows.map(r =>
    '<div class="info-row"><span class="k">' + r[0] + '</span><span class="v">' + r[1] + '</span></div>'
  ).join('') + '</div>';

  // Where a flight pickup meets the passenger. Choosing one also sets that
  // place's first-hour parking charge (server-side); a car park visit linked to
  // the order later overwrites both with what HKIA saw.
  let pointRow = '';
  if (p === 'ride' && isPickup) {
    const cur = o.pickup_point || '';
    const opts = PICKUP_POINTS.map(pt =>
      '<button class="pp-opt' + (pt === cur ? ' on' : '') + '" onclick="setPickupPoint(\'' + pt + '\')">' + pt + '</button>');
    // A car park HKIA reported that is not one of ours shows as it was named.
    if (cur && !PICKUP_POINTS.includes(cur)) opts.push('<span class="pp-opt on">' + esc(cur) + '</span>');
    pointRow = '<div class="pp-row"><span class="fk">上車點</span>' + opts.join('') + '</div>';
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
    const row = '<button class="field-row" onclick="editField(\'' + key + '\')">' +
      '<span class="fk">' + fLabel + '</span>' +
      '<span class="fv' + (unset ? ' unset' : '') + '">' + esc(value) + '</span>' +
      '<span class="chev">&#9998;</span></button>';
    // Waiving the parking fee is the everyday action (the pickup default often
    // turns out not to be charged), so it gets a one-tap pill; any other
    // amount is rare and goes through the numpad as usual.
    const lead = key === 'parking_fee' ? pointRow : '';
    if (key === 'parking_fee' && o.parking_fee > 0) {
      return lead + '<div class="fr-split">' + row +
        '<button class="fr-waive" onclick="waiveParking()">免</button></div>';
    }
    return lead + row;
  }).join('');

  sheet.insertAdjacentHTML('beforeend',
    sheetHead('<span class="badge ' + svcBadge(o) + '">' + label + '</span> ' +
      '<span style="font-variant-numeric:tabular-nums">' + esc(orderTime(o)) + '</span>',
      orderHost.subtitle ? orderHost.subtitle(o) : '#' + esc(shortId(o.order_id))) +
    info + fields +
    (p === 'ride' ? '<a class="tg-link" href="https://t.me/agent_ride_bot?start=order_' + encodeURIComponent(o.order_id) + '">喺 Telegram 開</a>' : '') +
    '<button class="cancel-link" onclick="openCancelConfirm()">取消訂單</button>'
  );
}

function editField(key) {
  const o = orderHost.order();
  if (!o) return;
  const fLabel = editableFields(o).find(f => f[0] === key)[1];
  if (key === 'time') {
    orderHost.push(numpadView({
      title: '改時間', hint: '而家 ' + orderTime(o), mode: 'time',
      onConfirm: v => orderHost.patch({ time: v }),
    }));
  } else {
    const quick = key === 'parking_fee' && o.parking_fee > 0
      ? { label: '免停車費', onTap: () => orderHost.patch({ parking_fee: 0 }) }
      : null;
    orderHost.push(numpadView({
      title: '改' + fLabel, hint: '而家 $' + $(o[key] || 0), mode: 'money', quick,
      onConfirm: v => orderHost.patch({ [key]: v }),
    }));
  }
}

function waiveParking() { orderHost.patch({ parking_fee: 0 }); }

const PICKUP_POINTS = ['P1', 'P4', '富豪'];

function setPickupPoint(pt) {
  const o = orderHost.order();
  if (!o || o.pickup_point === pt) return;
  orderHost.patch({ pickup_point: pt });
}

// Cancellation confirms in its own pushed view rather than an armed state on
// the same button: a page reload redraws the order's sheet, so a stacked view
// survives the SSE-driven re-renders that would wipe any in-place armed state
// mid-decision, and no disarm timer is needed at all.
function openCancelConfirm() { orderHost.push(cancelConfirmView); }

function cancelConfirmView(sheet) {
  const o = orderHost.order();
  if (!o) { orderHost.gone(); return; }
  const lines = [
    orderTime(o) + (o.passenger_name ? ' · ' + o.passenger_name : ''),
    o.pickup ? o.pickup + ' → ' + o.dropoff : '',
  ].filter(Boolean).map(esc).join('<br>');
  sheet.insertAdjacentHTML('beforeend',
    sheetHead('取消訂單', '#' + esc(shortId(o.order_id))) +
    '<div class="cancel-info">' + lines + '</div>' +
    '<button class="primary-btn danger" id="cancelGo">確認取消</button>' +
    '<button class="ghost-btn" onclick="popView()">返回</button>'
  );
  sheet.querySelector('#cancelGo').addEventListener('click', async function () {
    this.disabled = true;
    this.textContent = '取消緊…';
    const id = o.order_id;
    try {
      await apiWrite('PATCH', '/api/orders/' + encodeURIComponent(id), { status: 'cancelled' });
    } catch (e) {
      toast(e.message);
      this.disabled = false;
      this.textContent = '確認取消';
      return;
    }
    orderHost.cancelled(id);
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
function numpadView({ title, sub, hint, mode, onConfirm, quick, above, layout, suggest, skip }) {
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
      sheetHead(esc(title), sub || '') +
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
          '<div class="numpad-hint">' + esc(hint || '') + '</div>' +
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
        display.innerHTML = (input ? '$' + esc(input) : '<span class="dim">$0</span>') +
          (suggest != null && input === String(suggest) ? '<span class="sug-tag">建議</span>' : '');
      } else {
        const dim = '<span class="dim">&#8211;</span>';
        const digit = i => i < input.length ? esc(input[i]) : dim;
        display.innerHTML = digit(0) + digit(1) + ':' + digit(2) + digit(3);
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
