let state, page = 'TMS', selectedRequestId = null, selectedPlan = null, coaTimelineHours = 24;
const $ = s => document.querySelector(s),
  depts = {
    TMS: 'Engineering',
    SMMS: 'Signal & Telecommunication',
    TDMS: 'Traction Distribution'
  },
  places = {
    "Bengaluru East": ['Baiyappanahalli', 'Krishnarajapuram'],
    Yelahanka: ['Yelahanka East', 'Yelahanka North'],
    Hindupur: ['Hindupur North', 'Hindupur South'],
    Penukonda: ['Penukonda North', 'Penukonda South'],
    Dharmavaram: ['Dharmavaram West', 'Dharmavaram East']
  };
async function load() {
  state = await (await fetch('/api/state')).json();
  render()
}

function toast(s) {
  let e = $('#toast');
  e.textContent = s;
  e.className = 'show';
  setTimeout(() => e.className = '', 3000)
}

function badge(n) {
  return `<span class="risk ${n>88?'critical':n>70?'high':''}">ML RISK ${n}%</span>`
}

function options() {
  return Object.keys(places).map(x => `<option>${x}</option>`).join('')
}

function subs(st) {
  return places[st].map(x => `<option>${x}</option>`).join('')
}

function timelineMinutes(value) {
  const [hours, minutes] = (value || '00:00').split(':').map(Number);
  return hours * 60 + minutes;
}

function timelineMarkup(block, activities, passengerTrains, goodsTrains) {
  const blockStart = block ? timelineMinutes(block.start) : 0;
  const windowStart = coaTimelineHours === 24 ? 0 : Math.max(0, Math.floor(blockStart / 60) - Math.floor(coaTimelineHours / 2));
  const windowEnd = Math.min(1440, windowStart + coaTimelineHours * 60);
  const eventPosition = (start, end, label, tone, linked = false, title = label) => {
    const eventStart = timelineMinutes(start);
    const eventEnd = timelineMinutes(end || start) || eventStart + 15;
    if (eventEnd <= windowStart * 1 || eventStart >= windowEnd) return '';
    const left = Math.max(0, (eventStart - windowStart) / (windowEnd - windowStart) * 100);
    const width = Math.max(1.5, Math.min(100 - left, (eventEnd - eventStart) / (windowEnd - windowStart) * 100));
    return `<span class="timelineMarker ${tone}${linked ? ' linkedActivity' : ''}" style="left:${left}%;width:${width}%" title="${title}" aria-label="${title}">${label}</span>`;
  };
  const row = (label, items, tone, getStart, getEnd, linked = false, getLabel = item => item.name || item.work || item.id || getStart(item), getTitle = getLabel) => `<div class="timelineRow"><b>${label}</b><div class="timelineTrack">${items.map(item => eventPosition(getStart(item), getEnd(item), getLabel(item), tone, linked, getTitle(item))).join('') || '<i class="timelineEmpty">none</i>'}</div></div>`;
  const existingBlocks = state.blocks.filter(item => item.id !== (block && block.id) && item.status !== 'Completed' && item.status !== 'Cancelled');
  const tickCount = coaTimelineHours === 24 ? 4 : coaTimelineHours === 12 ? 4 : 3;
  const ticks = Array.from({ length: tickCount + 1 }, (_, index) => {
    const minutes = windowStart + (windowEnd - windowStart) * index / tickCount;
    return `<span>${String(Math.floor(minutes / 60) % 24).padStart(2, '0')}:${String(Math.floor(minutes % 60)).padStart(2, '0')}</span>`;
  }).join('');
  const recommended = block ? eventPosition(block.start, block.end, `${block.id} · ${block.start}–${block.end}`, 'recommendedWindow') : '<i class="timelineEmpty">none</i>';
  return `<div class="timelineControls"><span>Zoom</span>${[24, 12, 6].map(hours => `<button class="zoomButton ${coaTimelineHours === hours ? 'selected' : ''}" data-hours="${hours}">${hours}h</button>`).join('')}</div><div class="railTimeline" style="--timeline-columns:${tickCount + 1}"><div class="timelineAxis"><span></span>${ticks}</div>${row('Passenger trains', passengerTrains, 'passenger', item => item.time, item => item.time, false, item => item.no, item => `${item.no} · ${item.name}`)}${row('Goods trains', goodsTrains, 'goods', item => item.time, item => item.time, false, item => item.no, item => `${item.no} · ${item.name}`)}${row('Engineering maintenance', activities.filter(item => item.dept === 'TMS'), 'engineering', item => item.preferredStart, item => item.preferredEnd || item.preferredStart, true)}${row('S&T maintenance', activities.filter(item => item.dept === 'SMMS'), 'signalling', item => item.preferredStart, item => item.preferredEnd || item.preferredStart, true)}${row('Traction/OHE maintenance', activities.filter(item => item.dept === 'TDMS'), 'traction', item => item.preferredStart, item => item.preferredEnd || item.preferredStart, true)}${row('Existing blocks', existingBlocks, 'existingBlock', item => item.start, item => item.end)}<div class="timelineRow proposedRow"><b>AI recommended block</b><div class="timelineTrack">${recommended}</div></div></div>`;
}

function actionButtons(ids) {
  return `<div class="approvalActions"><button class="outline" data-requests="${ids.join(',')}" data-action="reject">Reject</button><button class="primary" data-requests="${ids.join(',')}" data-action="approve">Approve</button></div>`
}

function bindActions() {
  document.querySelectorAll('[data-requests]').forEach(group => group.onclick = () => group.dataset.requests.split(',').forEach(id => decision(id, group.dataset.action)))
}

function assignedBlockNotice(requests) {
  const assigned = state.blocks.filter(block => block.requests.some(id => requests.some(request => request.id === id)))
  if (!assigned.length) return '';
  return `<section class="card assignment"><p class="eyebrow">ASSIGNED BLOCK NOTIFICATION</p><h2>Your department's work window</h2>${assigned.map(block => `<div><b>${block.id} · ${block.start} – ${block.end}</b><span>${block.kmFrom}–${block.kmTo} km · ${block.status}</span><small>${requests.filter(request => block.requests.includes(request.id)).map(request => request.work).join(' · ')}</small></div>`).join('')}</section>`
}

async function refreshSelectedPlan() {
  if (!selectedRequestId) {
    selectedPlan = null;
    return null;
  }
  try {
    const response = await fetch('/api/requests/' + selectedRequestId + '/plan');
    if (!response.ok) {
      selectedPlan = { error: 'No AI recommendation yet' };
      return selectedPlan;
    }
    selectedPlan = await response.json();
    return selectedPlan;
  } catch (error) {
    selectedPlan = { error: 'AI planner unavailable' };
    return selectedPlan;
  }
}

function dept() {
  let mine = state.requests.filter(r => r.dept === page);
  if (!selectedRequestId && mine.length) selectedRequestId = mine[0].id;
  const selected = mine.find(r => r.id === selectedRequestId) || mine[0] || null;
  const metricOpen = mine.length;
  const metricHighRisk = mine.filter(r => r.risk >= 75).length;
  const metricHold = mine.filter(r => r.status === 'Coordination hold').length;
  const metricReady = mine.filter(r => r.status === 'Pending COA' || r.status === 'Approved').length;

  $('#eyebrow').textContent = 'ENGINEERING / TMS WORKSPACE';
  $('#title').textContent = 'Submit and monitor an engineering maintenance requirement';
  $('#content').innerHTML = `
    <div class="metricsRow">
      <div class="metricCard"><small>Open requests</small><strong>${metricOpen}</strong><span>maintenance queue</span></div>
      <div class="metricCard"><small>High risk</small><strong>${metricHighRisk}</strong><span>AI review needed</span></div>
      <div class="metricCard"><small>Coordination hold</small><strong>${metricHold}</strong><span>pending review</span></div>
      <div class="metricCard"><small>Ready for AI</small><strong>${metricReady}</strong><span>planned work</span></div>
    </div>
    <div class="engineeringLayout">
      <section class="card formCard">
        <div class="sectionHeader"><p class="eyebrow">NEW MAINTENANCE REQUEST</p></div>
        <h2>Engineering requirement</h2>
        <form id="request" class="engineeringForm">
          <div class="grid2">
            <label>Asset ID<input name="assetId" value="TRK-145" placeholder="e.g. TRK-145"></label>
            <label>Asset type<select name="assetType"><option>Track</option><option>OHE</option><option>Structure</option><option>Signal</option></select></label>
          </div>
          <div class="grid2">
            <label>Maintenance activity<input name="work" value="Rail geometry correction" required></label>
            <label>Safety priority<select name="priority"><option>Medium</option><option selected>High</option><option>Critical</option></select></label>
          </div>
          <label>Reason<textarea name="reason" required placeholder="Describe the defect and required work">Track geometry drift observed on the approach to Dharmavaram.</textarea></label>
          <div class="grid2">
            <label>From station<select id="fromStation" name="fromStation">${options()}</select></label>
            <label>From sub-station<select id="fromSub" name="fromSubStation">${subs('Bengaluru East')}</select></label>
          </div>
          <div class="grid2">
            <label>To station<select id="toStation" name="toStation"><option>Dharmavaram</option>${options()}</select></label>
            <label>To sub-station<select id="toSub" name="toSubStation">${subs('Dharmavaram')}</select></label>
          </div>
          <div class="grid2">
            <label>From KM<input name="kmFrom" type="number" step="0.1" value="121.4" required></label>
            <label>To KM<input name="kmTo" type="number" step="0.1" value="123.1" required></label>
          </div>
          <div class="grid2">
            <label>Preferred start<input name="preferredStart" type="time" value="01:00"></label>
            <label>Preferred end<input name="preferredEnd" type="time" value="05:00"></label>
          </div>
          <div class="grid2">
            <label>Duration<select name="duration"><option value="60">60 minutes</option><option value="90" selected>90 minutes</option><option value="120">120 minutes</option></select></label>
            <label>Safety clearance<select name="safetyClearanceRequired"><option value="true">YES</option><option value="false">NO</option></select></label>
          </div>
          <div class="grid2">
            <label>Request date<input name="day" type="date" value="2026-09-02" required></label>
            <label>Must complete by<input name="mustCompleteBy" type="date" value="2026-09-05" required></label>
          </div>
          <button class="primary">Submit to AI Planner</button>
        </form>
      </section>

      <section class="card resultCard">
        <div class="sectionHeader"><p class="eyebrow">AI PLANNING RESULT</p></div>
        <h2>${selected ? 'Selected requirement' : 'No selected requirement'}</h2>
        ${selected ? `
          <div class="requestSummary">
            <div class="summaryRow"><span>Request</span><strong>${selected.id}</strong></div>
            <div class="summaryRow"><span>Activity</span><strong>${selected.work}</strong></div>
            <div class="summaryRow"><span>Asset</span><strong>${selected.assetId || 'Not specified'} · ${selected.assetType || 'Track'}</strong></div>
            <div class="summaryRow"><span>Priority</span><strong>${badge(selected.risk)}</strong></div>
            <div class="summaryRow"><span>Status</span><strong class="status ${selected.status.toLowerCase().replace(/\s+/g, '-')}">${selected.status}</strong></div>
          </div>
          <div class="planPanel">
            ${selectedPlan && !selectedPlan.error ? `
              <div class="planHeader"><b>${selectedPlan.recommendedBlock.start} – ${selectedPlan.recommendedBlock.end}</b><span class="status recommended">${selectedPlan.risk} risk</span></div>
              <p>${selectedPlan.reason}</p>
              <ul>
                <li>Recommended action: ${selectedPlan.recommendation}</li>
                <li>Overlapping requests: ${selectedPlan.overlappingRequests && selectedPlan.overlappingRequests.length ? selectedPlan.overlappingRequests.join(', ') : 'None'}</li>
                <li>Available blocks: ${selectedPlan.availableBlocks && selectedPlan.availableBlocks.length ? selectedPlan.availableBlocks.map(block => block.id).join(', ') : 'No compatible blocks found'}</li>
              </ul>
            ` : `<p class="muted">Select a request to load the AI plan.</p>`}
          </div>
        ` : `<p class="muted">Submit an engineering requirement to generate the AI planning result.</p>`}
      </section>
    </div>

    <section class="card queueCard">
      <div class="sectionHeader"><p class="eyebrow">MAINTENANCE / PLANNING QUEUE</p></div>
      <h2>Engineering maintenance requests</h2>
      <div class="queueList">
        ${mine.length ? mine.map(r => `
          <button class="queueItem ${selected && selected.id === r.id ? 'selected' : ''}" data-request-id="${r.id}">
            <div>
              <strong>${r.work}</strong>
              <small>${r.assetId || 'Asset not specified'} · ${r.fromStation} → ${r.toStation}</small>
            </div>
            <div class="queueMeta">
              <span class="status ${r.status.toLowerCase().replace(/\s+/g, '-')}">${r.status}</span>
              <span>${r.kmFrom}–${r.kmTo} km</span>
            </div>
          </button>
        `).join('') : '<p class="muted">No engineering requests yet.</p>'}
      </div>
    </section>
  `;

  $('#request').onsubmit = submit;
  $('#fromStation').onchange = e => $('#fromSub').innerHTML = subs(e.target.value);
  $('#toStation').onchange = e => $('#toSub').innerHTML = subs(e.target.value);
  document.querySelectorAll('.queueItem').forEach(button => {
    button.onclick = async () => {
      selectedRequestId = button.dataset.requestId;
      await refreshSelectedPlan();
      render();
    };
  });
  if (selected) {
    refreshSelectedPlan();
  }
}
async function approveBlock(blockId) {
  const response = await fetch('/api/blocks/' + blockId, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ action: 'approve' })
  });
  if (!response.ok) return toast((await response.json()).detail || 'Block approval failed');
  toast('Block approved');
  await load();
}

function blockActivities(block, requests) {
  const assigned = requests.filter(request => block.requests.includes(request.id) || request.recommendedBlockId === block.id);
  return assigned.length ? assigned : requests.filter(request => request.fromKm < block.kmTo && request.toKm > block.kmFrom);
}

function coaDecision() {
  const requests = state.requests.filter(request => request.status !== 'Rejected' && request.status !== 'Completed');
  const blocks = state.blocks.filter(block => block.status !== 'Completed' && block.status !== 'Cancelled');
  const recommendedRequest = requests.find(request => request.recommendedBlockId && blocks.some(block => block.id === request.recommendedBlockId));
  const recommendedBlock = (recommendedRequest && blocks.find(block => block.id === recommendedRequest.recommendedBlockId)) || blocks.find(block => block.requests.length) || blocks[0];
  const activities = recommendedBlock ? blockActivities(recommendedBlock, requests) : [];
  const individualBlockTime = activities.reduce((total, request) => total + (request.duration || 0), 0);
  const combinedBlockTime = recommendedBlock ? recommendedBlock.durationMinutes : 0;
  const estimatedSavings = Math.max(0, individualBlockTime - combinedBlockTime);
  const passengerTrains = state.trains.filter(train => train.type !== 'Goods');
  const goodsTrains = state.trains.filter(train => train.type === 'Goods');
  const alternatives = blocks.filter(block => !recommendedBlock || block.id !== recommendedBlock.id).filter(block => !recommendedRequest || block.date === recommendedRequest.requestDate);
  const pending = requests.filter(request => request.status === 'Pending COA').length;
  const conflicts = recommendedBlock ? passengerTrains.filter(train => train.time >= recommendedBlock.start && train.time <= recommendedBlock.end) : [];
  const warning = conflicts.length ? `${conflicts.length} passenger movement${conflicts.length === 1 ? '' : 's'} intersect this window.` : 'No hard passenger-train conflict in the proposed window.';

  $('#eyebrow').textContent = 'CONTROL OFFICE APPLICATION / AI DECISION SUPPORT';
  $('#title').textContent = 'Which block should be approved, and why?';
  $('#content').innerHTML = `
    <div class="kpis coaKpis">
      <div><small>BLOCKS AVAILABLE</small><b>${blocks.length}</b><span>from COA state</span></div>
      <div><small>REQUESTS IN REVIEW</small><b>${pending}</b><span>awaiting approval</span></div>
      <div><small>ACTIVITIES BUNDLED</small><b>${activities.length}</b><span>in recommendation</span></div>
      <div><small>PASSENGER CONFLICTS</small><b>${conflicts.length}</b><span>hard constraints</span></div>
    </div>
    ${recommendedBlock ? `<section class="card recommendedBlock">
      <div class="decisionHeader"><div><p class="eyebrow">AI RECOMMENDED BLOCK</p><h2>${recommendedBlock.id} · ${recommendedBlock.start} – ${recommendedBlock.end}</h2><p class="muted">${recommendedBlock.kmFrom}–${recommendedBlock.kmTo} km · ${recommendedBlock.date} · ${activities.length} maintenance activit${activities.length === 1 ? 'y' : 'ies'}</p></div><span class="decisionScore">${recommendedRequest ? recommendedRequest.aiPriorityScore : recommendedBlock.aiScore}<small>decision score</small></span></div>
      <div class="decisionActions"><button class="primary" onclick="approveBlock('${recommendedBlock.id}')">Approve Block</button><button class="outline" onclick="document.getElementById('alternatives').scrollIntoView({behavior:'smooth'})">View Alternatives</button><button class="outline" onclick="page='TMS'; selectedRequestId='${recommendedRequest ? recommendedRequest.id : ''}'; render()">Modify</button><button class="outline dangerAction" onclick="${recommendedRequest ? `decision('${recommendedRequest.id}','reject')` : `toast('Select a request before rejecting')`}">Reject</button></div>
    </section>` : '<section class="card"><h2>No eligible block found</h2><p class="muted">The shared COA block inventory has no active candidate for the current maintenance requests.</p></section>'}

    <section class="card timelineCard"><div class="sectionHeader"><p class="eyebrow">RAILWAY TIMELINE</p><span class="muted">${recommendedRequest ? recommendedRequest.fromStation + ' → ' + recommendedRequest.toStation : 'Shared corridor'} · ${coaTimelineHours}-hour view</span></div>${timelineMarkup(recommendedBlock, activities, passengerTrains, goodsTrains)}<div class="timelineLegend"><span><i class="legendSwatch passenger"></i>Passenger</span><span><i class="legendSwatch goods"></i>Goods</span><span><i class="legendSwatch engineering"></i>Engineering</span><span><i class="legendSwatch signalling"></i>S&T</span><span><i class="legendSwatch traction"></i>Traction/OHE</span><span><i class="legendSwatch existingBlock"></i>Existing block</span><span><i class="legendSwatch recommendedWindow"></i>AI recommended</span></div></section>

    <section class="split coaDecisionGrid"><section class="card"><p class="eyebrow">WHY THIS BLOCK?</p><h2>Optimization rationale</h2><p class="decisionReason">${recommendedRequest ? recommendedRequest.aiRecommendation : 'The recommendation is derived from the shared COA block inventory and active maintenance requests.'}</p><ul class="reasonList"><li>Priority: ${recommendedRequest ? recommendedRequest.aiPriorityLevel + ' · ' + recommendedRequest.aiPriorityScore + '/99' : 'Shared block score'}</li><li>Safety: ${conflicts.length ? 'Passenger conflict requires review' : 'Hard passenger conflict cleared'}</li><li>Traffic and weather: considered by deterministic optimization rules</li><li>Bundling: ${activities.length} compatible activity${activities.length === 1 ? '' : 'ies'} selected</li></ul></section><section class="card"><p class="eyebrow">BUNDLED ACTIVITIES</p><h2>${activities.length ? 'Work proposed inside this block' : 'No bundled work'}</h2>${activities.length ? `<div class="bundleStats"><span>Individual block time <b>${individualBlockTime} min</b></span><span>Combined block time <b>${combinedBlockTime} min</b></span><span>Estimated savings <b>${estimatedSavings} min</b></span></div>${activities.map(request => `<div class="bundleItem"><span class="deptTag">${request.dept}</span><div><b>${request.work}</b><small>${request.fromKm}–${request.toKm} km · ${request.duration} min</small></div></div>`).join('')}` : '<p class="muted">Compatible activities will appear when requests share corridor and time.</p>'}</section></section>

    <section class="card" id="alternatives"><p class="eyebrow">ALTERNATIVE BLOCK OPTIONS</p><h2>Other shared COA blocks</h2><div class="alternativeList">${alternatives.length ? alternatives.map(block => `<div class="alternativeItem"><div><b>${block.id} · ${block.start} – ${block.end}</b><small>${block.kmFrom}–${block.kmTo} km · ${block.status}</small></div><span>${block.aiScore || 'n/a'} score</span></div>`).join('') : '<p class="muted">No alternative blocks in the current shared state.</p>'}</div></section>
    <section class="card warningCard"><p class="eyebrow">CONFLICTS AND OPERATIONAL WARNINGS</p><h2>${warning}</h2><p class="muted">Goods traffic is shown on the timeline and penalized during optimization; passenger movements are treated as hard constraints.</p></section>
    <section class="card"><p class="eyebrow">WHAT-IF SIMULATOR</p><h2>Test a changed possession window</h2><form id="simulation" class="form"><div class="twocol"><label>Block day<input name="day" type="date" value="${recommendedBlock ? recommendedBlock.date : '2026-09-03'}"></label><label>Start time<input name="start" type="time" value="${recommendedBlock ? recommendedBlock.start : '00:15'}"></label></div><div class="twocol"><label>Duration<select name="duration"><option value="60">60 minutes</option><option value="90" selected>90 minutes</option><option value="120">120 minutes</option></select></label><label>Traffic level<select name="trafficLevel"><option value="normal">Normal</option><option value="festival_peak">Festival / Peak</option></select></label></div><input type="hidden" name="kmFrom" value="${recommendedBlock ? recommendedBlock.kmFrom : 0}"><input type="hidden" name="kmTo" value="${recommendedBlock ? recommendedBlock.kmTo : 1}"><button class="outline">Run What-if</button></form><div id="simulationResult" class="simulationResult"><p class="muted">Change the time or duration to test feasibility against shared train and weather inputs.</p></div></section>
  `;
  $('#simulation').onsubmit = simulate;
  document.querySelectorAll('.zoomButton').forEach(button => button.onclick = () => {
    coaTimelineHours = Number(button.dataset.hours);
    coaDecision();
  });
}

function bundlingOpportunityMarkup(opportunities) {
  if (!opportunities.length) return '<p class="muted">No compatible multi-department opportunity is available in the shared planning state.</p>';
  return `<div class="opportunityList">${opportunities.map((opportunity, index) => `<button class="opportunityCard" data-opportunity-index="${index}"><div><b>${opportunity.proposedBlockTime}</b><small>${opportunity.corridor}</small></div><div class="opportunityDepartments">${opportunity.departments.join(' + ')}</div><div class="opportunityStats"><span>${opportunity.activityCount} activities</span><span>Score ${opportunity.aiScore}</span><span>${opportunity.combinedDuration} combined</span><span>${opportunity.estimatedBlockTimeSaving} saved</span></div><em>${opportunity.operationalImpact}</em></button>`).join('')}</div><div id="opportunityDetail" class="opportunityDetail"><p class="muted">Select an opportunity to inspect its maintenance activities and planning rationale.</p></div>`;
}

function showBundlingOpportunity(opportunity) {
  $('#opportunityDetail').innerHTML = `<div class="detailHeader"><div><p class="eyebrow">BUNDLING OPPORTUNITY DETAIL</p><h3>${opportunity.blockId} · ${opportunity.proposedBlockTime}</h3><p class="muted">${opportunity.corridor} · ${opportunity.departments.join(' + ')}</p></div><span class="decisionScore">${opportunity.aiScore}<small>AI score</small></span></div><p class="decisionReason">${opportunity.compatibilityReason}</p><div class="opportunityActivities">${opportunity.activities.map(activity => `<div class="opportunityActivity"><b>${activity.activity}</b><span>${activity.asset} · ${activity.department}</span><small>${activity.location} · ${activity.duration} min</small></div>`).join('')}</div><ul class="reasonList"><li>Train conflicts avoided: ${opportunity.trainConflictsAvoided.length ? opportunity.trainConflictsAvoided.join(', ') : 'No hard passenger conflicts'}</li><li>Why this window: ${opportunity.windowReason}</li></ul>`;
}

async function coa() {
  state = await (await fetch('/api/state')).json();
  return coaDecision();
}

async function aiDashboard() {
  const insights = await (await fetch('/api/insights?day=2026-09-03')).json();
  $('#eyebrow').textContent = 'AI DECISION SUPPORT';
  $('#title').textContent = 'COA Block Control';
  const opportunities = insights.bundlingOpportunities || [];
  $('#content').innerHTML = `<section class="card"><p class="eyebrow">AI BUNDLING OPPORTUNITIES</p><h2>Coordinate compatible maintenance in one possession</h2><p class="muted">Deterministic optimization using shared requests, COA blocks, train conflicts, traffic and weather inputs.</p>${bundlingOpportunityMarkup(opportunities)}</section><section class="card"><p class="eyebrow">ASSET HEALTH + FAILURE RISK</p><h2>OHE and track condition</h2>${insights.assets.map(a => `<div class="asset"><div><b>${a.name}</b><small>${a.type} · ${a.location}</small></div><span class="risk ${a.failureRisk >= 70 ? 'critical' : a.failureRisk >= 50 ? 'high' : ''}">${a.health}% health · ${a.failureRisk}% risk</span></div>`).join('')}</section><section class="card"><p class="eyebrow">WHAT-IF SIMULATION</p><h2>Test a proposed possession</h2><form id="simulation"><div class="twocol"><label>Day<input name="day" type="date" value="2026-09-03"></label><label>Start<input name="start" type="time" value="00:15"></label></div><div class="twocol"><label>Duration<select name="duration"><option value="60">60 minutes</option><option value="90">90 minutes</option><option value="120">120 minutes</option></select></label><label>Rainfall<input name="rainfallPercent" type="number" min="0" max="100" step="1" placeholder="Probability %"></label></div><button class="primary">Run simulation →</button></form><div id="simulationResult"></div></section>`;
  setupSimulationForm();
  document.querySelectorAll('.opportunityCard').forEach(card => card.onclick = () => showBundlingOpportunity(opportunities[Number(card.dataset.opportunityIndex)]));
  setupAssetLinks(insights.assets);
  arrangeWeatherMetrics();
}

function setupAssetLinks(assets) {
  document.querySelectorAll('.asset').forEach((element, index) => {
    const asset = assets[index];
    element.dataset.assetId = asset.id;
    element.tabIndex = 0;
    element.setAttribute('role', 'button');
    element.addEventListener('click', () => {
      page = 'ASSET';
      selectedAsset = asset.id;
      render();
    });
    element.addEventListener('keydown', event => {
      if (event.key === 'Enter' || event.key === ' ') element.click();
    });
  });
}

let selectedAsset;

async function assetDetails() {
  const insights = await (await fetch('/api/insights?day=2026-09-03')).json();
  const asset = insights.assets.find(item => item.id === selectedAsset) || insights.assets[0];
  $('#eyebrow').textContent = 'ASSET DETAILS';
  $('#title').textContent = asset.name;
  $('#content').innerHTML = `<button class="outline back" id="backToAssets">← Back to asset list</button><section class="card assetDetails"><p class="eyebrow">MONITORING RECORD</p><h2>${asset.id}</h2><div class="assetGrid"><div><small>ASSET ID</small><b>${asset.id}</b></div><div><small>SECTION ID</small><b>${asset.location}</b></div><div><small>ASSET TYPE</small><b>${asset.type}</b></div><div><small>TEMPERATURE</small><b>${asset.temperature} C</b></div><div><small>VIBRATION</small><b>${asset.vibration}</b></div><div><small>WEAR PERCENTAGE</small><b>${asset.wearPercentage}%</b></div><div><small>FAILURE HISTORY</small><b>${asset.failureHistory}</b></div><div><small>INSPECTION DATE</small><b>${asset.lastInspected}</b></div><div><small>ELECTRICAL LOAD</small><b>${asset.electricalLoad}%</b></div><div><small>LAST MAINTENANCE</small><b>${asset.lastMaintenance}</b></div></div></section>`;
  $('#backToAssets').onclick = () => {
    page = 'AI';
    render();
  };
}

let planningMode = 'weekly';

function planning() {
  const requests = state.requests.filter(request => request.status !== 'Rejected' && request.status !== 'Completed');
  const blocks = state.blocks.filter(block => block.status !== 'Completed' && block.status !== 'Cancelled');
  const corridors = [...new Set(requests.map(request => `${request.fromStation} – ${request.toStation}`))];
  const conflicts = request => state.trains.filter(train => train.type !== 'Goods' && train.time >= request.preferredStart && train.time <= request.preferredEnd).length;
  const weeklyRows = blocks.map(block => {
    const activities = requests.filter(request => block.requests.includes(request.id) || request.recommendedBlockId === block.id || (request.fromKm < block.toKm && request.toKm > block.fromKm));
    return `<div class="planningRow"><div><b>${block.id} · ${block.start}–${block.end}</b><small>${block.date} · ${block.kmFrom}–${block.kmTo} km</small></div><div>${activities.map(request => `<span class="planningActivity"><b>${request.work}</b><small>${request.department} · ${request.aiPriorityLevel} priority · ${request.duration} min</small></span>`).join('') || '<span class="muted">No linked maintenance</span>'}</div><span class="status ${block.status.toLowerCase().replace(/\s+/g, '-')}">${block.status}</span></div>`;
  }).join('');
  const critical = requests.filter(request => request.aiPriorityLevel === 'Critical' || request.aiPriorityScore >= 85);
  const highRisk = requests.filter(request => request.aiPriorityScore >= 70);
  const tentative = requests.filter(request => !request.recommendedBlockId);
  const blockState = block => ['Approved', 'Ongoing', 'Extended'].some(status => block.status.includes(status)) ? 'CONFIRMED' : 'TENTATIVE';
  const monthlyRows = requests.map(request => `<div class="planningBacklogRow"><div><b>${request.work}</b><small>${request.assetId || 'Asset not specified'} · ${request.department}</small></div><span>${request.fromStation} – ${request.toStation}</span><span class="status ${request.recommendedBlockId ? 'recommended' : ''}">${request.recommendedBlockId ? 'TENTATIVE' : 'TENTATIVE'}</span></div>`).join('');
  const criticalRows = critical.map(request => `<div class="planningBacklogRow"><div><b>${request.work}</b><small>${request.department} · ${request.assetId || 'Asset not specified'}</small></div><span>${request.mustCompleteBy}</span><span class="status tentative">TENTATIVE</span></div>`).join('');
  const riskRows = highRisk.filter(request => request.status !== 'Approved').map(request => `<div class="planningBacklogRow"><div><b>${request.work}</b><small>${request.department} · ${request.fromStation} – ${request.toStation}</small></div><span>${request.aiPriorityScore} risk</span><span class="status tentative">UNRESOLVED</span></div>`).join('');
  $('#eyebrow').textContent = 'PLANNING WORKSPACE';
  $('#title').textContent = planningMode === 'weekly' ? 'Executable block plan' : 'Maintenance outlook';
  $('#content').innerHTML = `<div class="planningModes"><button class="modeButton ${planningMode === 'weekly' ? 'selected' : ''}" data-mode="weekly">WEEKLY PLAN</button><button class="modeButton ${planningMode === 'monthly' ? 'selected' : ''}" data-mode="monthly">MONTHLY PLAN</button></div>${planningMode === 'weekly' ? `<div class="kpis planningKpis"><div><small>EXECUTABLE BLOCKS</small><b>${blocks.length}</b><span>shared COA blocks</span></div><div><small>MAINTENANCE TASKS</small><b>${requests.length}</b><span>active requirements</span></div><div><small>HIGH PRIORITY</small><b>${requests.filter(request => request.aiPriorityLevel === 'High' || request.aiPriorityLevel === 'Critical').length}</b><span>needs attention</span></div><div><small>CONFLICTS</small><b>${requests.reduce((total, request) => total + conflicts(request), 0)}</b><span>train movements</span></div></div><section class="card"><p class="eyebrow">WEEKLY PLAN · EXECUTABLE BLOCKS</p><h2>Detailed operational schedule</h2><p class="muted">Confirmed block records with linked maintenance work and current approval state.</p><div class="planningRows">${weeklyRows || '<p class="muted">No executable blocks in shared state.</p>'}</div></section>` : `<div class="kpis planningKpis"><div><small>BACKLOG</small><b>${requests.length}</b><span>active requirements</span></div><div><small>CRITICAL WORK</small><b>${critical.length}</b><span>upcoming attention</span></div><div><small>TENTATIVE OPPORTUNITIES</small><b>${tentative.length}</b><span>not exact schedule</span></div><div><small>UNRESOLVED HIGH RISK</small><b>${highRisk.filter(request => request.status !== 'Approved').length}</b><span>requires planning</span></div></div><section class="card monthlyNotice"><p class="eyebrow">MONTHLY PLAN · OUTLOOK</p><h2>Directional maintenance outlook</h2><p class="muted">This is a high-level planning view, not an exact operational schedule. Existing COA blocks are CONFIRMED only when approved or active; future work opportunities and backlog items remain TENTATIVE.</p><div class="planLegend"><span class="status confirmed">CONFIRMED</span><span class="status tentative">TENTATIVE</span></div></section><section class="card"><p class="eyebrow">HIGH-LEVEL MAINTENANCE BACKLOG</p><h2>Upcoming maintenance work</h2><div class="planningBacklog">${monthlyRows || '<p class="muted">No active maintenance backlog.</p>'}</div></section><section class="split"><section class="card"><p class="eyebrow">UPCOMING CRITICAL WORK</p><h2>Critical maintenance to watch</h2><div class="planningBacklog">${criticalRows || '<p class="muted">No upcoming critical work.</p>'}</div></section><section class="card"><p class="eyebrow">UNRESOLVED HIGH-RISK TASKS</p><h2>Requires planning attention</h2><div class="planningBacklog">${riskRows || '<p class="muted">No unresolved high-risk tasks.</p>'}</div></section></section><section class="split"><section class="card"><p class="eyebrow">CORRIDOR UTILIZATION</p><h2>Requests by corridor</h2>${corridors.map(corridor => `<div class="utilizationRow"><span>${corridor}</span><b>${requests.filter(request => `${request.fromStation} – ${request.toStation}` === corridor).length} tasks</b></div>`).join('') || '<p class="muted">No corridor activity.</p>'}</section><section class="card"><p class="eyebrow">TENTATIVE BLOCK OPPORTUNITIES</p><h2>Planning candidates</h2>${blocks.map(block => `<div class="utilizationRow"><span>${block.id} · ${block.start}–${block.end}</span><span class="status ${blockState(block) === 'CONFIRMED' ? 'confirmed' : 'tentative'}">${blockState(block)}</span></div>`).join('') || '<p class="muted">No block candidates.</p>'}</section></section>`}`;
  document.querySelectorAll('.modeButton').forEach(button => button.onclick = () => { planningMode = button.dataset.mode; planning(); });
}

function arrangeWeatherMetrics() {
  const weather = document.querySelector('.weather');
  if (!weather || weather.dataset.arranged) return;
  const metrics = [...weather.children];
  [0, 1, 4, 5, 2, 3].forEach((index, position) => {
    if (position % 2 === 0) {
      const row = document.createElement('div');
      row.append(metrics[index], metrics[index + 1]);
      weather.append(row);
    }
  });
  weather.dataset.arranged = 'true';
}

function setupSimulationForm() {
  const form = $('#simulation');
  form.insertAdjacentHTML('afterbegin', '<div class="twocol"><label>From KM<input name="kmFrom" type="number" step="0.1" value="121.4" required></label><label>To KM<input name="kmTo" type="number" step="0.1" value="123.1" required></label></div>');
  form.insertAdjacentHTML('afterbegin', '<div class="autoDetect"><b>Automatic planning</b><span id="autoStatus">Reading weather, traffic, and available time slots...</span><button type="button" class="outline" id="autoDetectButton">Detect conditions and optimize</button></div>');
  const rainfall = form.querySelector('[name="rainfallPercent"]');
  const rainfallLabel = rainfall.parentElement;
  rainfall.outerHTML = '<select name="weatherMode"><option value="normal" selected>Normal</option><option value="heavy_rain">Heavy Rain</option></select>';
  rainfallLabel.firstChild.nodeValue = 'Weather';
  rainfallLabel.insertAdjacentHTML('afterend', '<label>Train traffic<select name="trafficLevel"><option value="normal" selected>Normal</option><option value="festival_peak">Festival / Peak</option></select></label>');
  form.onsubmit = simulate;
  form.querySelectorAll('select, input').forEach(control => control.addEventListener('change', () => runSimulation(form)));
  $('#autoDetectButton').onclick = () => autoDetect(form, true);
  autoDetect(form, false);
}

async function autoDetect(form, notify) {
  const status = $('#autoStatus');
  const button = $('#autoDetectButton');
  status.textContent = 'Checking forecast and traffic data...';
  button.disabled = true;
  try {
    const response = await fetch('/api/insights?day=2026-09-03');
    if (!response.ok) throw new Error(`Planning data unavailable (${response.status})`);
    const insights = await response.json();
    const forecast = insights.weather;
    const window = insights.candidateWindows[0];
    if (!forecast || !window) throw new Error('No forecast or recommended time slot was returned');
    form.querySelector('[name="day"]').value = forecast.day;
    form.querySelector('[name="start"]').value = window.start;
    form.querySelector('[name="weatherMode"]').value = forecast.rainfallPercent >= 60 ? 'heavy_rain' : 'normal';
    form.querySelector('[name="trafficLevel"]').value = insights.demand && insights.demand.score >= 70 ? 'festival_peak' : 'normal';
    await runSimulation(form);
    status.textContent = `${forecast.condition} · ${forecast.rainfallPercent}% rain · ${window.start}-${window.end} recommended`;
    if (notify) toast(`Conditions detected. Recommended slot: ${window.start}–${window.end}`);
  } catch (error) {
    status.textContent = error.message;
    toast(error.message);
  } finally {
    button.disabled = false;
  }
}

async function simulate(e) {
  e.preventDefault();
  runSimulation(e.target);
}

async function runSimulation(form) {
  const body = Object.fromEntries(new FormData(form));
  body.kmFrom = +body.kmFrom;
  body.kmTo = +body.kmTo;
  body.duration = +body.duration;
  body.rainfallPercent = body.weatherMode === 'heavy_rain' ? 70 : 8;
  delete body.weatherMode;
  const response = await fetch('/api/simulate', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  const result = await response.json();
  if (!response.ok) return toast(result.detail || 'Simulation could not be completed');
  const previous = form.dataset.lastPlan ? JSON.parse(form.dataset.lastPlan) : null;
  form.dataset.lastPlan = JSON.stringify(result);
  $('#simulationResult').innerHTML = `${previous ? `<div class="simulationPlan"><small>CURRENT PLAN</small><b>${previous.start} – ${previous.end}</b><span>Score: ${previous.score}</span></div><p class="simulationChange">↓ Changed simulation inputs</p>` : ''}<div class="simulationPlan new"><small>${previous ? 'NEW PLAN' : 'CURRENT PLAN'}</small><b>${result.start} – ${result.end}</b><span>Score: ${result.score}</span></div><p class="muted">${result.explanation}${result.conflicts.length ? ' ' + result.conflicts.map(c => c.trainNo + ' at ' + c.time).join(', ') : ''}</p>`;
}
async function manual(e) {
  e.preventDefault();
  let b = Object.fromEntries(new FormData(e.target));
  b.kmFrom = +b.kmFrom;
  b.kmTo = +b.kmTo;
  b.duration = +b.duration;
  let r = await fetch('/api/manual-blocks', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json'
    },
    body: JSON.stringify(b)
  });
  if (!r.ok) return toast((await r.json()).detail);
  toast('COA block created');
  load()
}
async function decision(id, action) {
  const reason = action === 'reject' ? window.prompt('Reason for rejection:') : null;
  if (action === 'reject' && !reason) return;
  const response = await fetch('/api/requests/' + id, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json'
    },
    body: JSON.stringify({
      action,
      reason
    })
  });
  if (!response.ok) return toast((await response.json()).detail || 'Action failed');
  toast('Request ' + action + 'd');
  load()
}

function render() {
  document.querySelectorAll('nav button').forEach(b => b.classList.toggle('selected', b.dataset.page === page));
  page === 'COA' ? coa() : page === 'AI' ? aiDashboard() : page === 'PLANNING' ? planning() : page === 'ASSET' ? assetDetails() : dept()
}
document.querySelectorAll('nav button').forEach(b => b.onclick = () => {
  page = b.dataset.page;
  render()
});
window.decision = decision;
load();
