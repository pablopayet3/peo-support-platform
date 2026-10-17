/* Northwind Benefits sidebar app. Calls the integration service through Zendesk's secure request proxy,
   so the API key never reaches the browser. Northwind PEO is a fictional company. */

/* Zendesk returns a date field as the agent's local midnight in UTC (2026-10-06T04:00:00.000Z in New York).
   Turn it back into the calendar day the agent picked. */
function day(v) {
  if (!v || /^\d{4}-\d{2}-\d{2}$/.test(v)) return v;
  const d = new Date(v);
  if (isNaN(d)) return v;
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
}

const client = ZAFClient.init();
const $ = (html) => { document.getElementById('app').innerHTML = html; };
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
let S = {};
let T = {};

const RESULT = {
  ai_intake_complete: ['Complete', 'ok'], ai_intake_incomplete: ['Incomplete', 'warn'],
  ai_fail_identity: ['Failed: identity not confirmed', 'bad'], ai_fail_no_plan: ['Failed: no active plan', 'bad'],
  ai_fail_window: ['Failed: outside enrollment window', 'bad'],
};

// What to do now is read live from the Confluence procedure, through the integration service (/v1/guidance).
// A step that has a Tango walkthrough carries its "Show me how" link. GUIDE is only the copy shown when
// the integration service itself cannot be reached.
const GUIDE = {
  ai_intake_complete: [
    'If the plan asks for proof of birth, check that the date on it matches the Event date.',
    'Confirm the baby’s legal name with the parent.',
    'Approve below. The enrollment is created as pending with the carrier.',
  ],
  ai_intake_incomplete: [
    'Run the checks below. If a system was down, they work again once it is back.',
    'Ask the parent only for what is still missing.',
  ],
  ai_fail_identity: [
    'Do not share any account detail until the requester is verified.',
    'Ask them to log in, or to write from the email address on file.',
    'If an admin writes for an employee, confirm they are an admin of that company.',
  ],
  ai_fail_no_plan: [
    'Check whether the employee waived coverage or has an enrollment in progress.',
    'Explain the options. A new enrollment is only possible inside the 30-day window.',
  ],
  ai_fail_window: [
    'The window has closed. Check today whether the carrier allows an exception.',
    'Tell Benefits Escalation the same day, and keep the parent informed.',
  ],
};

function rid() { return 'zaf-' + Math.random().toString(16).slice(2, 14); }

function api(path, method, body) {
  return client.request({
    url: S.integration_base_url + path, type: method, contentType: 'application/json',
    headers: { Authorization: 'Bearer {{setting.integration_api_key}}', 'X-Request-Id': rid() },
    secure: true, cors: false, data: body ? JSON.stringify(body) : undefined,
  });
}

function countdown(iso) {
  const ms = new Date(iso) - new Date();
  if (ms <= 0) return ['Deadline passed', 'bad'];
  const days = Math.floor(ms / 864e5); const hours = Math.floor((ms % 864e5) / 36e5);
  return [`${days} days ${hours} h left`, days < 3 ? 'bad' : days < 10 ? 'warn' : 'ok'];
}

function field(id) { return `ticket.customField:custom_field_${id}`; }

async function load() {
  const meta = await client.metadata();
  S = meta.settings;
  const keys = ['ticket.id', 'ticket.requester.email', 'ticket.requester.id', 'currentUser.id', field(S.field_life_event_type),
    field(S.field_ai_intake_result), field(S.field_event_date), field(S.field_enrollment_outcome), field(S.field_enrollment_id)];
  const d = await client.get(keys);
  if (!load.listening) {
    load.listening = true;
    [S.field_life_event_type, S.field_event_date, S.field_enrollment_id].forEach((id) =>
      client.on(`ticket.custom_field_${id}.changed`, () => load()));
  }
  T = {
    id: d['ticket.id'], email: d['ticket.requester.email'], me: d['currentUser.id'], phone: null,
    lifeEvent: d[field(S.field_life_event_type)], result: d[field(S.field_ai_intake_result)],
    eventDate: day(d[field(S.field_event_date)]), outcome: d[field(S.field_enrollment_outcome)], enrollmentId: d[field(S.field_enrollment_id)],
  };
  if (!T.lifeEvent) { $('<p class="muted">Not a life-event ticket.</p>'); return; }
  if (!T.email) {
    // A caller known only by phone number (voice tickets)
    try { T.phone = (await client.request(`/api/v2/users/${d['ticket.requester.id']}.json`)).user.phone; } catch (e) { /* shown as identity not confirmed */ }
  }
  await render();
}

/* The client company as Sales and the CSM see it (Salesforce). Context only: never blocks anything. */
function accountBox(acc) {
  if (!acc || acc.status === 'not_configured' || acc.status === 'not_found') return '';
  if (acc.status !== 'ok') return '<p class="muted">Salesforce is not answering, so account details are hidden.</p>';
  const r = acc.renewal;
  const money = (n) => (n == null ? '' : `$${Number(n).toLocaleString('en-US')}`);
  const renewal = r ? `<div class="row"><span>Renewal</span><span class="badge ${r.days_away < 90 ? 'warn' : 'info'}">${esc(r.date)} · ${money(r.amount)}</span></div>` : '';
  return `<h3>Account <span class="src">Salesforce</span></h3><div class="card">
    <div class="row"><span>Client</span><b>${esc(acc.name)}</b></div>
    <div class="row"><span>Account owner</span><span>${esc(acc.owner || '-')}</span></div>
    <div class="row"><span>Size</span><span>${esc(acc.employees ?? '-')} employees · ${esc(acc.industry || '')}</span></div>
    ${renewal}
    <a class="more" href="${esc(acc.url)}" target="_blank" rel="noopener">Open in Salesforce ↗</a></div>`;
}

async function render(extra = '') {
  const eventType = String(T.lifeEvent).replace('qle_', '');
  let check = null; let summary = null; let error = '';
  if (T.eventDate) {
    try {
      check = await api('/v1/qle/check', 'POST', { requester: T.email ? { email: T.email } : { phone: T.phone }, event_type: eventType, event_date: T.eventDate });
      if (check.employee_id) summary = await api(`/v1/employees/${check.employee_id}/summary`, 'GET');
    } catch (e) {
      error = e.status === 503 ? 'A system is not answering. Try again in a minute.' : `Could not run the checks (${e.status || 'error'}).`;
    }
  }
  const result = (check && check.ai_intake_result) || T.result || 'ai_intake_incomplete';
  const [label, cls] = RESULT[result] || [result, 'info'];
  const dl = check && check.deadline ? countdown(check.deadline) : null;
  let g = null;
  try { g = await api(`/v1/guidance?situation=${encodeURIComponent(result)}&event_type=${encodeURIComponent(eventType)}`, 'GET'); } catch (e) { /* built-in copy below */ }
  const steps = g && g.steps.length ? g.steps : (GUIDE[result] || []).map((text) => ({ text }));
  const guide = steps.map((st) => `<li>${esc(st.text)}${st.how_to ? ` <a class="how" href="${esc(st.how_to.url)}" target="_blank" rel="noopener">${esc(st.how_to.label)} ↗</a>` : ''}</li>`).join('');
  const page = g && g.page;
  const when = page && page.updated_at ? new Date(page.updated_at).toLocaleDateString('en-US', { month: 'short', day: 'numeric' }) : '';
  const source = g && g.source === 'confluence' ? `Live from Confluence · version ${esc(page.version)}${when ? `, edited ${esc(when)}` : ''}`
    : g && g.source === 'confluence_cached' ? 'From Confluence (last copy read: Confluence is not answering)'
    : 'Built-in copy: Confluence could not be read';
  const procUrl = (page && page.url) || S.procedure_url;
  const NAME = { newborn: "Baby's", adoption: "Child's", marriage: "Spouse's", loss_of_coverage: "Dependent's" }[eventType] || "Dependent's";
  const checks = check ? check.checks.map((c) => `<li><b>${c.passed ? '✓' : '✗'}</b>${esc(c.name.replace('_', ' '))}: ${esc(c.detail)}</li>`).join('') : '';
  const plan = summary && summary.medical_plan ? `${esc(summary.medical_plan.name)} · ${esc(summary.medical_plan.coverage_level.replace('_', ' '))}` : 'None';
  const approved = T.outcome === 'qle_approved' || T.enrollmentId;
  $(`
    <div class="card">
      <div class="row"><span>Life event</span><b>${esc(eventType)}, ${esc(T.eventDate || 'date missing')}</b></div>
      <div class="row"><span>Checks</span><span class="badge ${cls}">${esc(label)}</span></div>
      ${dl ? `<div class="row"><span>Deadline</span><span class="badge ${dl[1]}">${dl[0]}</span></div>` : ''}
    </div>
    ${error ? `<p class="err">${esc(error)}</p>` : ''}
    ${summary ? `<h3>Employee</h3><div class="card">
      <div class="row"><span>Name</span><b>${esc(summary.name)}</b></div>
      <div class="row"><span>Company</span><span>${esc(summary.company_name)}</span></div>
      <div class="row"><span>Medical</span><span>${plan}</span></div>
      <div class="row"><span>Dependents</span><span>${summary.dependents.length}</span></div></div>` : ''}
    ${accountBox(summary && summary.account)}
    ${checks ? `<h3>What was checked</h3><div class="card"><ul class="checks">${checks}</ul></div>` : ''}
    <h3>What to do now</h3><div class="card"><ol class="guide">${guide}</ol>
      <p class="src">${source}</p>
      ${procUrl ? `<a class="more" href="${esc(procUrl)}" target="_blank" rel="noopener">Full procedure in Confluence ↗</a>` : ''}</div>
    ${approved ? `<div class="card"><span class="badge ok">Approved</span> Enrollment <b>${esc(T.enrollmentId || '')}</b></div>` : ''}
    ${!approved && result === 'ai_intake_complete' ? `<h3>Approve the enrollment</h3><div class="card">
      <label for="fn">${NAME} legal first name</label><input id="fn" autocomplete="off">
      <label for="ln">Last name</label><input id="ln" autocomplete="off">
      <label for="dob">Date of birth</label><input id="dob" type="date" value="${eventType === 'newborn' ? esc(T.eventDate || '') : ''}">
      <button class="primary" id="approve">Approve enrollment</button><div id="formErr" class="err"></div></div>` : ''}
    ${extra}`);
  const btn = document.getElementById('approve');
  if (btn) btn.addEventListener('click', () => confirmApprove(check, eventType));
}

function confirmApprove(check, eventType) {
  const fn = document.getElementById('fn').value.trim();
  const ln = document.getElementById('ln').value.trim();
  const dob = document.getElementById('dob').value;
  if (!fn || !ln || !dob) { document.getElementById('formErr').textContent = 'All three fields are required.'; return; }
  if (eventType === 'newborn' && dob !== T.eventDate) { document.getElementById('formErr').textContent = 'The date of birth differs from the Event date. Correct the Event date first.'; return; }
  const m = document.getElementById('modal');
  m.innerHTML = `<div class="box"><b>Approve this enrollment?</b><p>${esc(fn)} ${esc(ln)} (date of birth ${esc(dob)}) will be added to
    ${esc(check.company_name)}'s plan as a pending enrollment with the carrier. This is recorded with your name.</p>
    <div class="actions"><button id="no">Cancel</button><button class="primary" id="yes" style="width:auto;margin:0">Approve</button></div></div>`;
  m.classList.remove('hidden');
  document.getElementById('no').onclick = () => m.classList.add('hidden');
  document.getElementById('yes').onclick = async () => {
    document.getElementById('yes').disabled = true;
    try {
      const r = await api('/v1/enrollments', 'POST', {
        ticket_id: T.id, employee_id: check.employee_id, event_type: eventType, event_date: T.eventDate,
        dependent: { first_name: fn, last_name: ln, date_of_birth: dob }, approver_zendesk_user_id: T.me,
      });
      T.enrollmentId = r.enrollment_id; T.outcome = 'qle_approved';
      m.classList.add('hidden');
      client.invoke('notify', `Enrollment ${r.enrollment_id} created`, 'notice');
      await render();
    } catch (e) {
      m.classList.add('hidden');
      const msg = e.status === 403 ? 'Only Benefits Ops specialists can approve.' : e.status === 409 ? 'The checks no longer pass. Run them again.' : 'Approval failed. Try again; it will not create a duplicate.';
      document.getElementById('formErr').textContent = msg;
    }
  };
}

/* Grow the sidebar frame with its content, so the approval form is never cut off. */
let lastHeight = 0;
function fit() {
  const h = Math.ceil(document.documentElement.scrollHeight) + 8;
  if (Math.abs(h - lastHeight) > 4) { lastHeight = h; client.invoke('resize', { width: '100%', height: `${h}px` }); }
}
new ResizeObserver(fit).observe(document.body);

client.on('app.registered', () => { fit(); load(); });
