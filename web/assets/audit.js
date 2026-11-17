// Workflow 2: the population-level fairness audit.
//
// Also owns the tab switch between the two workflows, because the audit view is
// the only one that needs lazy loading when it is first opened.
(() => {
  const POLL_MS = 2000;
  const shell = document.querySelector("#shell");
  const tabs = document.querySelectorAll(".tab");
  const views = { evaluate: document.querySelector("#view-evaluate"), audit: document.querySelector("#view-audit") };

  const modeSwitch = document.querySelector("#mode-switch");
  const modeNote = document.querySelector("#mode-note");
  const confirmBox = document.querySelector("#audit-confirm");
  const presetGrid = document.querySelector("#preset-grid");
  const startButton = document.querySelector("#audit-start");
  const validateButton = document.querySelector("#validate-start");
  const jobBox = document.querySelector("#audit-job");
  const validationBox = document.querySelector("#validation");
  const runSelect = document.querySelector("#run-select");
  const results = document.querySelector("#audit-results");

  let initialised = false;
  let selectedPreset = null;
  let presets = [];
  let llm = null;
  let mode = "offline";
  const plans = {};
  let pollTimer = null;
  let lastJobId = null;
  let currentRun = null;

  // ------------------------------------------------------------------ helpers
  const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;",
  }[char]));
  const human = (value) => String(value ?? "").replaceAll("_", " ");
  const isNum = (value) => typeof value === "number" && Number.isFinite(value);
  const fixed = (value, digits = 2) => (isNum(value) ? value.toFixed(digits) : "—");
  const pct = (value, digits = 1) => (isNum(value) ? `${(value * 100).toFixed(digits)}%` : "—");
  const count = (value) => (isNum(value) ? value.toLocaleString() : "—");

  function formatValue(value, unit) {
    if (!isNum(value)) return "—";
    if (unit === "score_points") return `${value.toFixed(2)} pts`;
    if (String(unit).startsWith("fraction") || unit === "rate") return pct(value);
    return value.toFixed(3);
  }

  // Estimates are rounded to the nearest 5 minutes; measured times stay exact.
  function estimate(minutes) {
    if (!isNum(minutes)) return "unknown time";
    const rounded = Math.round(minutes / 5) * 5;
    if (rounded < 5) return "under 5 min";
    const hours = Math.floor(rounded / 60);
    const rest = rounded % 60;
    return hours ? `~${hours} h${rest ? ` ${rest} min` : ""}` : `~${rest} min`;
  }

  function duration(seconds) {
    if (!isNum(seconds)) return "—";
    const total = Math.max(0, Math.round(seconds));
    const minutes = Math.floor(total / 60);
    return minutes ? `${minutes}m ${String(total % 60).padStart(2, "0")}s` : `${total}s`;
  }

  function verdictPill(passed, labels = ["Pass", "Fail", "Info"]) {
    if (passed === true) return `<span class="pill pass">${labels[0]}</span>`;
    if (passed === false) return `<span class="pill fail">${labels[1]}</span>`;
    return `<span class="pill info">${labels[2]}</span>`;
  }

  async function api(path, options) {
    const response = await fetch(path, options);
    const text = await response.text();
    let data;
    try {
      data = JSON.parse(text);
    } catch {
      throw new Error(`The audit service failed (${response.status}).`);
    }
    if (!response.ok) throw new Error(data.detail || `Request failed (${response.status}).`);
    return data;
  }

  // --------------------------------------------------------------------- tabs
  function showView(name) {
    const view = views[name] ? name : "evaluate";
    Object.entries(views).forEach(([key, element]) => element.classList.toggle("hidden", key !== view));
    tabs.forEach((tab) => {
      const active = tab.dataset.view === view;
      tab.classList.toggle("active", active);
      if (active) tab.setAttribute("aria-current", "page");
      else tab.removeAttribute("aria-current");
    });
    shell.classList.toggle("wide", view === "audit");
    if (view === "audit" && !initialised) {
      initialised = true;
      initAudit();
    }
  }
  window.addEventListener("hashchange", () => showView(location.hash.slice(1)));
  showView(location.hash.slice(1));

  // ------------------------------------------------------------ modes/presets
  const modelName = () => (llm ? `${llm.provider} / ${llm.model}` : "the evaluator's model");
  const available = (preset) => mode === "offline" || preset.llm;

  function setMode(next) {
    mode = next === "llm" && llm && llm.configured ? "llm" : "offline";
    modeSwitch.querySelectorAll(".mode").forEach((button) => {
      const active = button.dataset.mode === mode;
      button.classList.toggle("selected", active);
      button.setAttribute("aria-checked", String(active));
    });
    if (!presets.find((p) => p.key === selectedPreset && available(p))) {
      const first = presets.find(available);
      selectedPreset = first ? first.key : null;
    }
    hideConfirm();
    modeNote.innerHTML = mode === "llm"
      ? `Scores with <strong>${esc(modelName())}</strong>, the model your evaluator uses. Each design shows its exact number of API calls before anything starts; audits above ${count(llm.max_calls)} calls are blocked here. Repeating an unchanged design later is served from cache and costs nothing.`
      : "Scores with the offline scorer, which has no bias by construction and deliberate random noise. Use it to check that the audit stays quiet when there is nothing to find. It says nothing about any real model.";
    renderPresets();
    if (mode === "llm") loadPlans();
  }

  function costLine(preset) {
    if (mode === "offline") return `<span class="preset-cost free">Free · ${estimate(preset.estimate_minutes)}</span>`;
    if (!preset.llm) return '<span class="preset-cost muted">Offline only</span>';
    const plan = plans[`${preset.key}|llm`];
    if (!plan || plan === "loading") return '<span class="preset-cost muted">Counting calls…</span>';
    if (plan.error) return `<span class="preset-cost over">${esc(plan.error)}</span>`;
    const label = `~${count(plan.api_calls)} calls · ${estimate(plan.estimated_minutes)}`;
    return plan.within_limit
      ? `<span class="preset-cost">${label}</span>`
      : `<span class="preset-cost over">${label} · over the ${count(plan.max_calls)}-call limit</span>`;
  }

  function renderPresets() {
    presetGrid.innerHTML = presets.map((preset) => {
      const disabled = !available(preset);
      return `
      <button type="button" class="preset${preset.key === selectedPreset ? " selected" : ""}" role="radio"
        aria-checked="${preset.key === selectedPreset}" data-preset="${esc(preset.key)}"${disabled ? " disabled" : ""}>
        <span class="preset-head"><strong>${esc(preset.label)}</strong>${costLine(preset)}</span>
        <span class="preset-corpus">${esc(preset.corpus)}</span>
        <span class="preset-desc">${esc(preset.description)}</span>
      </button>`;
    }).join("");
    presetGrid.querySelectorAll(".preset:not([disabled])").forEach((button) => button.addEventListener("click", () => {
      selectedPreset = button.dataset.preset;
      hideConfirm();
      renderPresets();
    }));
  }

  async function loadPlan(key) {
    const id = `${key}|llm`;
    if (plans[id] && plans[id] !== "loading") return plans[id];
    plans[id] = "loading";
    try {
      plans[id] = await api("/api/audit/plan", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ preset: key, mode: "llm" }),
      });
    } catch (error) {
      plans[id] = { error: error.message };
    }
    if (mode === "llm") renderPresets();
    return plans[id];
  }

  async function loadPlans() {
    // One at a time: each plan is a dry run in its own process.
    for (const preset of presets.filter((p) => p.llm)) {
      if (mode !== "llm") return;
      await loadPlan(preset.key);
    }
  }

  modeSwitch.querySelectorAll(".mode").forEach((button) => button.addEventListener("click", () => setMode(button.dataset.mode)));

  // ------------------------------------------------------------------ confirm
  function hideConfirm() {
    confirmBox.classList.add("hidden");
    confirmBox.innerHTML = "";
  }

  async function confirmLlmRun(preset) {
    confirmBox.innerHTML = '<p class="job-note">Counting the calls this audit needs…</p>';
    confirmBox.classList.remove("hidden");
    const plan = plans[`${preset.key}|llm`] && plans[`${preset.key}|llm`] !== "loading"
      ? plans[`${preset.key}|llm`] : await loadPlan(preset.key);
    if (plan.error) {
      confirmBox.innerHTML = `<div class="error">${esc(plan.error)}</div>`;
      return;
    }
    if (!plan.within_limit) {
      confirmBox.innerHTML = `<div class="error">${esc(preset.label)} needs about ${count(plan.api_calls)} API calls, above the ${count(plan.max_calls)}-call limit for audits started here. Choose the Quick audit, raise <code>OPENMERIT_AUDIT_MAX_CALLS</code>, or run it from the CLI.</div>`;
      return;
    }
    const arms = Object.entries(plan.mitigation_calls || {}).filter(([, n]) => n);
    confirmBox.innerHTML = `
      <h3>Spend about ${count(plan.api_calls)} API calls?</h3>
      <p>The <strong>${esc(preset.label)}</strong> design scores ${count(plan.n_pairs)} candidate/job pairs with <strong>${esc(plan.provider)} / ${esc(plan.model)}</strong>:
        ${count(plan.baseline_calls)} calls for the original resumes and ${count(plan.variant_calls)} for the variants${arms.length ? `, plus ${arms.map(([name, n]) => `${count(n)} for ${esc(human(name))}`).join(", ")}` : ""}.
        ${plan.served_from_cache ? `${count(plan.served_from_cache)} of those repeat earlier prompts and come from the cache. ` : ""}
        At ${count(plan.requests_per_minute)} requests a minute that takes ${estimate(plan.estimated_minutes)}.</p>
      <div class="confirm-actions">
        <button class="primary" id="confirm-start" type="button">Start: spend ~${count(plan.api_calls)} calls</button>
        <button class="text-button neutral" id="confirm-cancel" type="button">Not now</button>
      </div>`;
    document.querySelector("#confirm-cancel").addEventListener("click", hideConfirm);
    document.querySelector("#confirm-start").addEventListener("click", () => {
      hideConfirm();
      startJob({ kind: "pipeline", preset: preset.key, mode: "llm", confirmed_calls: plan.api_calls });
    });
  }

  // ---------------------------------------------------------------------- job
  function setBusy(busy) {
    startButton.disabled = busy;
    validateButton.disabled = busy;
  }

  function renderJob(job) {
    if (!job) {
      jobBox.classList.add("hidden");
      setBusy(false);
      return;
    }
    const running = job.state === "running";
    setBusy(running);
    const title = job.kind === "validation"
      ? "Auditor validation · offline"
      : job.mode === "llm"
        ? `${job.preset_label || "Audit"} · ${job.provider} / ${job.model}`
        : `Tool check · ${job.preset_label || "Audit"}`;
    const stateLabel = { running: "Running", complete: "Complete", failed: "Failed", cancelled: "Cancelled" }[job.state] || job.state;
    const stages = (job.stages || []).map((stage) => `
      <li class="stage ${stage.state}"><span class="stage-icon" aria-hidden="true"></span>${esc(stage.label)}${stage.state === "stopped" ? ` <em>(${esc(job.state)} here)</em>` : ""}</li>`).join("");
    const doneCount = (job.stages || []).filter((stage) => stage.state === "done").length;
    const total = (job.stages || []).length;
    const progress = job.progress && running && (job.stages || []).some((s) => s.state === "running" && s.key.startsWith("score"))
      ? ` Scored ${count(job.progress.done)} of ${count(job.progress.total)}${job.progress.arm && job.progress.arm !== "baseline" ? ` (${human(job.progress.arm)})` : ""}.`
      : "";
    const planned = job.mode === "llm" && job.planned_calls ? ` Planned: about ${count(job.planned_calls)} API calls.` : "";
    const note = job.kind === "validation"
      ? "Runs a no-bias null and a known injected bias with the offline scorer, then grades whether the suite told them apart."
      : (total ? `${doneCount} of ${total} stages complete.${progress}${planned}` : "");
    jobBox.innerHTML = `
      <div class="job-head">
        <div>
          <span class="result-label">${esc(title)}</span>
          <strong class="job-state ${esc(job.state)}">${esc(stateLabel)}</strong>
        </div>
        <div class="job-time">${duration(job.elapsed_s)} elapsed${running && isNum(job.estimate_minutes) ? ` · ${estimate(job.estimate_minutes)} in total` : ""}</div>
      </div>
      ${running ? `<div class="progress"><i style="width:${total ? Math.max(4, (doneCount / total) * 100) : 100}%"${total ? "" : ' class="indeterminate"'}></i></div>` : ""}
      ${note ? `<p class="job-note">${esc(note)}</p>` : ""}
      ${stages ? `<ol class="stages">${stages}</ol>` : ""}
      ${job.error ? `<pre class="job-error">${esc(job.error)}</pre>` : ""}
      ${running ? '<button class="text-button" id="audit-cancel" type="button">Cancel run</button>' : ""}`;
    jobBox.classList.remove("hidden");
    const cancel = document.querySelector("#audit-cancel");
    if (cancel) cancel.addEventListener("click", cancelJob);
  }

  async function pollJob() {
    clearTimeout(pollTimer);
    try {
      const { job } = await api("/api/audit/job");
      renderJob(job);
      if (job && job.state === "running") {
        pollTimer = setTimeout(pollJob, POLL_MS);
        return;
      }
      if (job && job.job_id !== lastJobId) {
        lastJobId = job.job_id;
        if (job.kind === "validation" && job.validation) renderValidation(job.validation);
        if (job.kind === "pipeline" && job.state === "complete") await loadRuns(job.run_id);
      }
    } catch (error) {
      jobBox.innerHTML = `<div class="error">${esc(error.message)}</div>`;
      jobBox.classList.remove("hidden");
      setBusy(false);
    }
  }

  async function startJob(body) {
    setBusy(true);
    try {
      const { job } = await api("/api/audit/job", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      lastJobId = null;
      renderJob(job);
      pollTimer = setTimeout(pollJob, POLL_MS);
    } catch (error) {
      jobBox.innerHTML = `<div class="error">${esc(error.message)}</div>`;
      jobBox.classList.remove("hidden");
      setBusy(false);
    }
  }

  async function cancelJob() {
    try {
      const { job } = await api("/api/audit/job/cancel", { method: "POST" });
      clearTimeout(pollTimer);
      renderJob(job);
    } catch (error) {
      jobBox.insertAdjacentHTML("afterbegin", `<div class="error">${esc(error.message)}</div>`);
    }
  }

  startButton.addEventListener("click", () => {
    const preset = presets.find((p) => p.key === selectedPreset);
    if (!preset) return;
    if (mode === "llm") confirmLlmRun(preset);
    else startJob({ kind: "pipeline", preset: preset.key, mode: "offline" });
  });
  validateButton.addEventListener("click", () => startJob({ kind: "validation" }));

  // --------------------------------------------------------------- validation
  function renderValidation(verdict) {
    if (!verdict) {
      validationBox.classList.add("hidden");
      return;
    }
    const checks = (verdict.checks || []).map((check) => `
      <li>${verdictPill(check.passed)}<div><strong>${esc(human(check.check))}</strong><p>${esc(check.explanation)}</p></div></li>`).join("");
    validationBox.innerHTML = `
      <div class="validation-head">
        <div>
          <span class="result-label">Auditor self-check${verdict.validated_at ? ` · ${esc(new Date(verdict.validated_at).toLocaleString())}` : ""}</span>
          <h3>${esc(verdict.n_passed)}/${esc(verdict.n_checks)} checks passed</h3>
        </div>
        ${verdictPill(verdict.passed, ["Trustworthy", "Not trustworthy", "Unknown"])}
      </div>
      <p>${esc(verdict.interpretation)}</p>
      <details><summary>Show the checks</summary><ul class="checks">${checks}</ul></details>`;
    validationBox.classList.remove("hidden");
  }

  // --------------------------------------------------------------------- runs
  function runLabel(run) {
    const when = run.created_at ? new Date(run.created_at).toLocaleString() : run.run_id;
    const design = run.preset_label || human(run.run_name);
    return run.mode === "llm" ? `${design} · ${run.model} · ${when}` : `Tool check · ${design} · ${when}`;
  }

  async function loadRuns(preferred) {
    try {
      const { runs } = await api("/api/audit/runs");
      if (!runs.length) {
        runSelect.innerHTML = "<option>No finished runs</option>";
        runSelect.disabled = true;
        results.innerHTML = `<div class="empty">No finished audits yet. Start the <strong>Offline demo</strong> above; it takes about two minutes.</div>`;
        return;
      }
      runSelect.innerHTML = runs.map((run) => `<option value="${esc(run.run_id)}">${esc(runLabel(run))}</option>`).join("");
      runSelect.disabled = false;
      const target = runs.some((run) => run.run_id === preferred) ? preferred
        : (runs.some((run) => run.run_id === currentRun) ? currentRun : runs[0].run_id);
      runSelect.value = target;
      await loadRun(target);
    } catch (error) {
      results.innerHTML = `<div class="error">${esc(error.message)}</div>`;
    }
  }

  async function loadRun(runId) {
    currentRun = runId;
    results.innerHTML = '<div class="empty">Loading run…</div>';
    try {
      const run = await api(`/api/audit/runs/${encodeURIComponent(runId)}`);
      if (currentRun === runId) renderRun(run);
    } catch (error) {
      results.innerHTML = `<div class="error">${esc(error.message)}</div>`;
    }
  }
  runSelect.addEventListener("change", () => loadRun(runSelect.value));

  // ---------------------------------------------------------------- rendering
  const fileUrl = (run, name) => `/api/audit/runs/${encodeURIComponent(run.run_id)}/report/${encodeURIComponent(name)}`;

  function tiles(run) {
    const items = [
      ["Resumes × jobs", `${count(run.n_base_resumes)} × ${count(run.n_jobs)}`],
      ...(isNum(run.n_focus_pairs) ? [["Borderline pairs audited", count(run.n_focus_pairs)]] : []),
      ...(run.mode === "llm" ? [["API calls made", count(run.api_calls)]] : []),
      ["Resume variants", count(run.n_variants)],
      ["Scores recorded", count(run.n_records)],
      ["Metrics evaluated", count(run.n_metrics)],
      ["Failed thresholds", count(run.n_failed)],
      ["Significant after FDR", count(run.n_significant_after_fdr)],
      ["Significant and above threshold", count(run.n_material)],
      ["Metric conflicts", count(run.n_conflicts)],
    ];
    return `<div class="tiles">${items.map(([label, value]) => `<div><span>${esc(label)}</span><strong>${esc(value)}</strong></div>`).join("")}</div>`;
  }

  function trust(run) {
    const sanity = run.sanity_check;
    const reliability = run.reliability || {};
    const means = (sanity && sanity.mean_score_by_expected_fit) || {};
    const sanityBody = sanity ? `
      <div class="trust-card">
        <div class="trust-head"><h4>Does the scorer discriminate?</h4>${verdictPill(sanity.verdict === "pass")}</div>
        <div class="fit-means">${["weak", "moderate", "strong"].filter((k) => k in means).map((k) => `<div><span>${k} fit</span><strong>${fixed(means[k], 1)}</strong></div>`).join("")}</div>
        <p>Kendall τ ${fixed(sanity.kendall_tau)} against graded fit, strong minus weak gap ${fixed(sanity.strong_minus_weak_gap, 1)} points. A bias number from a scorer that cannot separate strong from weak candidates would be noise.</p>
      </div>` : "";
    const ratio = reliability.cue_effect_to_noise_ratio;
    return `
      <div class="trust">
        ${sanityBody}
        <div class="trust-card">
          <div class="trust-head"><h4>How noisy is the scorer?</h4>${isNum(ratio) ? verdictPill(ratio < 1, ["Effects within noise", "Effects exceed noise", "Info"]) : ""}</div>
          <div class="fit-means">
            <div><span>Noise floor SD</span><strong>${fixed(reliability.noise_floor_sd)}</strong></div>
            <div><span>Cue effect ÷ noise</span><strong>${fixed(ratio)}</strong></div>
            <div><span>ICC (resume)</span><strong>${fixed(reliability.icc1_resume, 3)}</strong></div>
          </div>
          <p>Repeated scoring of identical prompts sets the noise floor. A cue effect below it cannot be told apart from decoding noise.</p>
        </div>
      </div>`;
  }

  function cueEffects(run) {
    const rows = run.cue_effects || [];
    if (!rows.length) return "";
    const noise = (run.reliability || {}).noise_floor_sd;
    const scale = Math.max(
      ...rows.map((row) => Math.max(row.swing_ci_high || 0, row.swing || 0, row.swing_threshold || 0)),
      isNum(noise) ? noise : 0,
      0.5,
    ) * 1.1;
    const at = (value) => `${Math.min(100, (value / scale) * 100)}%`;
    const body = rows.map((row) => `
      <div class="cue-row" title="${esc(row.interpretation)}">
        <span class="cue-name">${esc(human(row.cue))}</span>
        <div class="cue-track">
          <i class="cue-bar ${row.swing_passed === false ? "over" : ""}" style="width:${at(row.swing || 0)}"></i>
          ${isNum(row.swing_threshold) ? `<b class="marker threshold" style="left:${at(row.swing_threshold)}"></b>` : ""}
          ${isNum(noise) ? `<b class="marker noise" style="left:${at(noise)}"></b>` : ""}
        </div>
        <span class="cue-value">${fixed(row.swing)} pts</span>
        <span class="cue-flip">${pct(row.flip_rate)}</span>
      </div>`).join("");
    return `
      <div class="result-section">
        <h3>What each cue does to the score</h3>
        <p class="section-note">Mean score swing when only that cue changes. <b class="legend threshold"></b> pass threshold · <b class="legend noise"></b> noise floor. The last column is the share of candidates whose decision flipped.</p>
        <div class="cue-chart">
          <div class="cue-row cue-header"><span>Cue family</span><span></span><span>Swing</span><span>Flips</span></div>
          ${body}
        </div>
      </div>`;
  }

  function groupGaps(run) {
    const rows = run.group_gaps || [];
    if (!rows.length) return "";
    return `
      <div class="result-section">
        <h3>Score gaps between groups</h3>
        <div class="table-wrap"><table>
          <thead><tr><th>Attribute</th><th>Largest gap</th><th>95% CI</th><th>q (BH)</th><th>Verdict</th></tr></thead>
          <tbody>${rows.map((row) => `<tr title="${esc(row.interpretation)}">
            <td>${esc(human(row.attribute))}</td>
            <td>${fixed(row.gap, 3)} pts</td>
            <td>${isNum(row.ci_low) ? `${fixed(row.ci_low, 3)} to ${fixed(row.ci_high, 3)}` : "—"}</td>
            <td>${fixed(row.q_value, 3)}</td>
            <td>${row.significant ? '<span class="pill fail">Significant</span>' : verdictPill(row.passed)}</td>
          </tr>`).join("")}</tbody>
        </table></div>
      </div>`;
  }

  function findings(run) {
    const rows = run.headline_findings || [];
    if (!rows.length) return "";
    return `
      <div class="result-section">
        <h3>Headline findings</h3>
        <p class="section-note">Ranked by consequence: a changed decision outranks a significant sub-point score gap.</p>
        <div class="findings">${rows.map((row) => `
          <article class="finding">
            <div class="finding-head">
              <span>${esc(human(row.metric))}</span>
              <span class="finding-value">${esc(formatValue(row.value, row.unit))} ${verdictPill(row.verdict === "PASS" ? true : row.verdict === "FAIL" ? false : null)}</span>
            </div>
            <p>${esc(row.interpretation)}</p>
          </article>`).join("")}</div>
      </div>`;
  }

  function families(run) {
    const entries = Object.entries(run.by_family || {});
    if (!entries.length) return "";
    return `
      <div class="result-section">
        <h3>Metric families</h3>
        <div class="table-wrap"><table>
          <thead><tr><th>Family</th><th>Metrics</th><th>Failed threshold</th><th>Significant after FDR</th></tr></thead>
          <tbody>${entries.map(([name, row]) => `<tr><td>${esc(human(name))}</td><td>${count(row.n)}</td><td>${count(row.failed)}</td><td>${count(row.significant)}</td></tr>`).join("")}</tbody>
        </table></div>
        ${(run.skipped_families || []).length ? `<p class="section-note">Skipped: ${esc(JSON.stringify(run.skipped_families))}</p>` : ""}
      </div>`;
  }

  function mitigation(run) {
    const arms = (run.mitigation || {}).arms || [];
    if (!arms.length) return "";
    return `
      <div class="result-section">
        <h3>Mitigation: what helps, and what it costs</h3>
        <div class="table-wrap"><table>
          <thead><tr><th>#</th><th>Strategy</th><th>Verdict</th><th>Metrics better / worse</th><th>Rank agreement</th><th>Top-k overlap</th><th>Score spread</th><th>Sent to human</th></tr></thead>
          <tbody>${arms.map((arm) => `<tr title="${esc((arm.verdict_reasons || []).join(" "))}">
            <td>${esc(arm.rank ?? "—")}</td>
            <td>${esc(human(arm.arm))}</td>
            <td><span class="pill ${arm.verdict === "recommended" ? "pass" : "info"}">${esc(human(arm.verdict))}</span></td>
            <td>${count(arm.n_metrics_improved)} / ${count(arm.n_metrics_worsened)}</td>
            <td>${fixed(arm.rank_agreement)}</td>
            <td>${fixed(arm.top_k_overlap)}</td>
            <td>${fixed(arm.score_dispersion_ratio)}×</td>
            <td>${count(arm.n_routed_to_human)}</td>
          </tr>`).join("")}</tbody>
        </table></div>
        ${run.mitigation.guidance ? `<p class="section-note">${esc(run.mitigation.guidance)}</p>` : ""}
      </div>`;
  }

  function conflicts(run) {
    const rows = run.conflicts || [];
    if (!rows.length) return "";
    return `
      <div class="result-section">
        <h3>Where the metrics disagree</h3>
        ${run.conflict_guidance ? `<p class="section-note">${esc(run.conflict_guidance)}</p>` : ""}
        <div class="conflicts">${rows.map((row) => `
          <details class="conflict">
            <summary><span class="pill ${row.severity === "critical" ? "fail" : row.severity === "important" ? "warn" : "info"}">${esc(row.severity)}</span>
              ${esc(human(row.type))}${row.attribute ? ` · ${esc(human(row.attribute))}` : ""}</summary>
            <p>${esc(row.explanation)}</p>
            ${row.recommendation ? `<p><strong>Recommendation.</strong> ${esc(row.recommendation)}</p>` : ""}
          </details>`).join("")}</div>
      </div>`;
  }

  function unstable(run) {
    const cards = run.cards_summary;
    if (!cards || !(cards.unstable_candidates || []).length) return "";
    return `
      <div class="result-section">
        <h3>Candidates whose decision depends on a cue</h3>
        <p class="section-note">${count(cards.n_decision_unstable)} of ${count(cards.n_cards)} candidate/job pairs (${pct(cards.fraction_decision_unstable)}) have a score band that crosses a decision boundary across their variants.</p>
        <div class="table-wrap"><table>
          <thead><tr><th>Resume</th><th>Job</th><th>Score band across variants</th><th>Boundary crossed</th></tr></thead>
          <tbody>${cards.unstable_candidates.map((row) => `<tr><td>${esc(row.resume_id)}</td><td>${esc(row.job_id)}</td>
            <td>${(row.band || []).map((v) => fixed(v, 1)).join(" to ")}</td><td>${esc(row.boundary)}</td></tr>`).join("")}</tbody>
        </table></div>
      </div>`;
  }

  function plots(run) {
    if (!(run.plots || []).length && !run.has_html_report) return "";
    return `
      <div class="result-section">
        <div class="plots-head">
          <h3>Report figures</h3>
          ${run.has_html_report ? `<a class="secondary-action" href="${fileUrl(run, "report.html")}" target="_blank" rel="noopener">Open full HTML report ↗</a>` : ""}
        </div>
        <div class="plots">${(run.plots || []).map((name) => `
          <a href="${fileUrl(run, name)}" target="_blank" rel="noopener"><img loading="lazy" src="${fileUrl(run, name)}" alt="${esc(human(name.replace(/\.png$/, "")))}"><span>${esc(human(name.replace(/\.png$/, "")))}</span></a>`).join("")}</div>
      </div>`;
  }

  function allMetrics(run) {
    const metrics = run.metrics || [];
    if (!metrics.length) return "";
    const familiesList = [...new Set(metrics.map((m) => m.family))];
    return `
      <div class="result-section">
        <div class="plots-head">
          <h3>Every metric</h3>
          <div class="metric-filters">
            <select id="metric-family" aria-label="Metric family"><option value="">All families</option>${familiesList.map((f) => `<option value="${esc(f)}">${esc(human(f))}</option>`).join("")}</select>
            <label><input type="checkbox" id="metric-failed"> Failed only</label>
          </div>
        </div>
        <div class="table-wrap tall"><table id="metric-table">
          <thead><tr><th>Metric</th><th>Value</th><th>Threshold</th><th>q (BH)</th><th>Verdict</th></tr></thead>
          <tbody></tbody>
        </table></div>
      </div>`;
  }

  function bindMetricFilters(run) {
    const family = document.querySelector("#metric-family");
    const failed = document.querySelector("#metric-failed");
    const body = document.querySelector("#metric-table tbody");
    if (!family || !body) return;
    const draw = () => {
      const rows = (run.metrics || []).filter((m) => (!family.value || m.family === family.value) && (!failed.checked || m.passed === false));
      body.innerHTML = rows.map((m) => `<tr title="${esc(m.interpretation)}">
        <td>${esc(m.metric)}</td><td>${esc(formatValue(m.value, m.unit))}</td>
        <td>${esc(isNum(m.threshold) ? formatValue(m.threshold, m.unit) : "—")}</td>
        <td>${fixed(m.q_value, 3)}</td><td>${m.significant ? '<span class="pill fail">Significant</span>' : verdictPill(m.passed)}</td></tr>`).join("")
        || '<tr><td colspan="5">No metrics match.</td></tr>';
    };
    family.addEventListener("change", draw);
    failed.addEventListener("change", draw);
    draw();
  }

  function renderRun(run) {
    const offline = run.provider === "mock";
    results.innerHTML = `
      <div class="run-badge"><span class="badge ${offline ? "tool" : "llm"}">${offline ? "Tool check · offline scorer" : "LLM audit"}</span></div>
      <div class="run-meta">
        <div><span>Scorer</span><strong>${esc(run.provider)} / ${esc(run.model)}</strong></div>
        <div><span>Corpus</span><strong>${esc(run.corpus)}</strong></div>
        <div><span>Cue families</span><strong>${esc((run.cue_families || []).map(human).join(", ") || "—")}</strong></div>
        <div><span>Run time</span><strong>${duration(run.elapsed_s)}</strong></div>
      </div>
      ${offline ? `<div class="note"><strong>Tool check.</strong> These numbers describe the offline scorer, which has no bias by construction and deliberate random noise, not a real model. A healthy audit shows <strong>0 significant and above threshold</strong> and a non-zero noise floor. Threshold "fails" here are mostly that noise near the decision cut-offs, and a "significant" effect of a few hundredths of a point is the offline scorer reacting to slightly longer text.</div>`
        : `<div class="note llm"><strong>LLM audit of ${esc(run.provider)} / ${esc(run.model)}.</strong> Start with <strong>Significant and above threshold</strong>: effects that are unlikely to be chance after correcting for the number of tests <em>and</em> large enough to matter. A threshold "fail" on its own can be noise, and a significant but tiny effect can be harmless; compare both with the noise floor below.</div>`}
      ${(run.design_notes || []).map((n) => `<div class="note">${esc(n)}</div>`).join("")}
      ${(run.warnings || []).length ? `<div class="fallback"><strong>Run warnings.</strong> ${run.warnings.map(esc).join(" ")}</div>` : ""}
      ${tiles(run)}
      ${trust(run)}
      ${cueEffects(run)}
      ${findings(run)}
      ${groupGaps(run)}
      ${unstable(run)}
      ${mitigation(run)}
      ${conflicts(run)}
      ${families(run)}
      ${plots(run)}
      ${allMetrics(run)}
      <p class="privacy">Run <code>${esc(run.run_id)}</code>. Population-level evidence for system review; it is not a statement about any individual candidate.</p>`;
    bindMetricFilters(run);
  }

  // --------------------------------------------------------------------- init
  async function initAudit() {
    try {
      const data = await api("/api/audit/presets");
      presets = data.presets || [];
      llm = data.llm || null;
      const llmButton = modeSwitch.querySelector('[data-mode="llm"]');
      if (llm && llm.configured) {
        document.querySelector("#mode-llm-model").textContent = `${llm.provider} / ${llm.model}`;
      } else {
        llmButton.disabled = true;
        document.querySelector("#mode-llm-model").textContent = llm
          ? `Add the ${llm.provider} API key to .env and restart to enable`
          : "Not configured";
      }
      setMode(llm && llm.configured ? "llm" : "offline");
    } catch (error) {
      presetGrid.innerHTML = `<div class="error">${esc(error.message)}</div>`;
    }
    api("/api/audit/validation").then((data) => renderValidation(data.validation)).catch(() => {});
    await loadRuns();
    pollJob();
  }
})();
