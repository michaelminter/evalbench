// evalbench frontend: form helpers on the new-eval page, live SSE updates on the eval page.
(() => {
  "use strict";

  const FINAL = new Set(["done", "error", "cancelled"]);
  const ACTIVITY = "▸ ";

  // ------------------------------------------------------------ rendering helpers

  function renderMarkdown(root) {
    root.querySelectorAll(".md[data-src]").forEach((el) => {
      const src = el.dataset.src;
      el.removeAttribute("data-src");
      if (window.marked && window.DOMPurify) {
        el.innerHTML = DOMPurify.sanitize(marked.parse(src, { gfm: true, breaks: false }));
      } else {
        el.textContent = src;
        el.classList.add("plain");
      }
    });
  }

  function appendTranscript(pre, text) {
    // Activity lines (tool calls, commands) get their own styling.
    const parts = text.split(/(^▸ .*$\n?)/m);
    for (const part of parts) {
      if (!part) continue;
      if (part.startsWith(ACTIVITY)) {
        const span = document.createElement("span");
        span.className = "activity";
        span.textContent = part;
        pre.appendChild(span);
      } else {
        pre.appendChild(document.createTextNode(part));
      }
    }
  }

  function setTranscript(pre, text) {
    pre.textContent = "";
    appendTranscript(pre, text);
  }

  function renderTranscripts(root) {
    root.querySelectorAll("pre.transcript[data-src]").forEach((pre) => {
      const src = pre.dataset.src;
      pre.removeAttribute("data-src");
      setTranscript(pre, src);
    });
  }

  function renderDiffs(root) {
    root.querySelectorAll("pre.diff[data-src]").forEach((pre) => {
      const lines = pre.dataset.src.split("\n");
      pre.removeAttribute("data-src");
      const frag = document.createDocumentFragment();
      for (const line of lines) {
        const span = document.createElement("span");
        if (line.startsWith("diff --git")) span.className = "d-file";
        else if (line.startsWith("+++") || line.startsWith("---")) span.className = "d-meta";
        else if (line.startsWith("@@")) span.className = "d-hunk";
        else if (line.startsWith("+")) span.className = "d-add";
        else if (line.startsWith("-")) span.className = "d-del";
        span.textContent = line + "\n";
        frag.appendChild(span);
      }
      pre.appendChild(frag);
    });
  }

  function fillEffortSelect(modelSelect, effortSelect) {
    const opt = modelSelect.selectedOptions[0];
    const efforts = opt && opt.dataset.efforts ? JSON.parse(opt.dataset.efforts) : [];
    const want = effortSelect.value || effortSelect.dataset.initial || "high";
    effortSelect.innerHTML = "";
    for (const e of efforts) {
      const o = document.createElement("option");
      o.value = o.textContent = e;
      effortSelect.appendChild(o);
    }
    effortSelect.value = efforts.includes(want) ? want : efforts[efforts.length - 1] || "default";
    effortSelect.disabled = efforts.length === 0;
  }

  function wireJudgeSelects(root) {
    root.querySelectorAll("#judge-select, .judge-select-inline").forEach((sel) => {
      const effort = sel.id === "judge-select"
        ? document.getElementById("judge-effort")
        : sel.parentElement.querySelector(".judge-effort-inline");
      if (!effort || sel.dataset.wired) return;
      sel.dataset.wired = "1";
      sel.addEventListener("change", () => fillEffortSelect(sel, effort));
      fillEffortSelect(sel, effort);
    });
  }

  function formatElapsed(sec) {
    if (sec < 60) return `${sec.toFixed(0)}s`;
    const m = Math.floor(sec / 60), s = Math.floor(sec % 60);
    return m < 60 ? `${m}m ${String(s).padStart(2, "0")}s` : `${Math.floor(m / 60)}h ${m % 60}m`;
  }

  function wireTabs(root) {
    const cards = [...root.querySelectorAll(".card")];
    if (root.matches?.(".card")) cards.push(root);
    cards.forEach((card) => {
      if (card.dataset.tabsWired) return;
      card.dataset.tabsWired = "1";
      card.querySelector(".tabs")?.addEventListener("click", (e) => {
        const btn = e.target.closest(".tab");
        if (!btn) return;
        card.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t === btn));
        card.querySelectorAll(".tab-panel").forEach((p) => p.classList.toggle("active", p.dataset.panel === btn.dataset.tab));
      });
    });
  }

  function applyScores(root) {
    const data = root.querySelector?.(".scores-data") || document.querySelector(".scores-data");
    if (!data) return;
    let parsed;
    try { parsed = JSON.parse(data.textContent); } catch { return; }
    for (const [runId, sc] of Object.entries(parsed.scores || {})) {
      const badge = document.querySelector(`#run-${CSS.escape(runId)} .score-badge`);
      if (!badge) continue;
      badge.hidden = false;
      badge.innerHTML = "";
      const s = document.createElement("span");
      s.className = "score";
      s.style.setProperty("--s", sc.score);
      s.textContent = String(+sc.score);
      s.title = sc.rationale || "";
      badge.appendChild(s);
    }
  }

  function enhance(root) {
    renderMarkdown(root);
    renderTranscripts(root);
    renderDiffs(root);
    wireTabs(root);
    wireJudgeSelects(root);
    applyScores(root);
  }

  // ------------------------------------------------------------ theme

  function initTheme() {
    const btn = document.getElementById("theme-toggle");
    if (!btn) return;
    const root = document.documentElement;
    const sync = () => btn.setAttribute("aria-checked", String(root.dataset.theme === "dark"));

    btn.addEventListener("click", () => {
      root.dataset.theme = root.dataset.theme === "dark" ? "light" : "dark";
      try { localStorage.setItem("theme", root.dataset.theme); } catch { /* storage unavailable */ }
      sync();
    });

    // Follow OS changes until the user has picked a theme explicitly.
    matchMedia("(prefers-color-scheme: dark)").addEventListener("change", (e) => {
      let saved = null;
      try { saved = localStorage.getItem("theme"); } catch { /* storage unavailable */ }
      if (saved) return;
      root.dataset.theme = e.matches ? "dark" : "light";
      sync();
    });

    sync();
  }

  // ------------------------------------------------------------ new-eval page

  function initForm() {
    const form = document.getElementById("eval-form");
    if (!form) return;

    const count = document.getElementById("run-count");
    const updateCount = () => {
      const n = form.querySelectorAll('input[name="combo"]:checked').length;
      const reps = Math.max(1, parseInt(form.repeats.value, 10) || 1);
      count.textContent = `${n * reps} run${n * reps === 1 ? "" : "s"}` + (reps > 1 ? ` (${n} × ${reps})` : "");
    };

    const updateMode = () => {
      const mode = form.querySelector('input[name="mode"]:checked').value;
      form.querySelectorAll("[data-show-when]").forEach((el) => (el.hidden = el.dataset.showWhen !== mode));
      form.querySelectorAll("[data-mode-hint]").forEach((el) => (el.hidden = el.dataset.modeHint !== mode));
    };

    form.addEventListener("change", (e) => {
      if (e.target.name === "mode") updateMode();
      updateCount();
    });
    form.repeats.addEventListener("input", updateCount);

    form.addEventListener("click", (e) => {
      const row = e.target.closest(".row-toggle");
      const col = e.target.closest(".col-toggle");
      if (!row && !col) return;
      const boxes = row
        ? [...row.closest("tr").querySelectorAll('input[name="combo"]:not(:disabled)')]
        : [...col.closest("table").querySelectorAll(`input[name="combo"][data-effort="${CSS.escape(col.dataset.effort)}"]:not(:disabled)`)];
      const turnOn = boxes.some((b) => !b.checked);
      boxes.forEach((b) => (b.checked = turnOn));
      updateCount();
    });

    // Custom model rows.
    const addRow = (provEl, modelId, checked = []) => {
      const table = provEl.querySelector("table.grid");
      if (table.querySelector(`tr[data-model="${CSS.escape(modelId)}"]`)) return;
      const efforts = [...table.querySelectorAll("thead .col-toggle")].map((b) => b.dataset.effort);
      const tr = document.createElement("tr");
      tr.dataset.model = modelId;
      const th = document.createElement("th");
      const btn = document.createElement("button");
      btn.type = "button"; btn.className = "row-toggle custom"; btn.textContent = modelId;
      th.appendChild(btn); tr.appendChild(th);
      for (const eff of efforts) {
        const td = document.createElement("td");
        const cb = document.createElement("input");
        cb.type = "checkbox"; cb.name = "combo"; cb.dataset.effort = eff;
        cb.value = `${provEl.dataset.provider}|${modelId}|${eff}`;
        cb.checked = checked.includes(cb.value);
        td.appendChild(cb); tr.appendChild(td);
      }
      table.querySelector("tbody").appendChild(tr);
    };

    form.querySelectorAll(".add-model-btn").forEach((btn) => {
      const provEl = btn.closest(".provider");
      const input = provEl.querySelector(".custom-model");
      const add = () => {
        const id = input.value.trim();
        if (!id || /[|\s]/.test(id)) return;
        addRow(provEl, id, [`${provEl.dataset.provider}|${id}|default`]);
        input.value = "";
        updateCount();
      };
      btn.addEventListener("click", add);
      input.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); add(); } });
    });

    // Restore prefilled combos for models not in the catalog.
    try {
      const pre = JSON.parse(document.getElementById("prefill-combos").innerHTML || "[]");
      for (const v of pre) {
        if (form.querySelector(`input[name="combo"][value="${CSS.escape(v)}"]`)) continue;
        const [prov, model] = v.split("|");
        const provEl = form.querySelector(`.provider[data-provider="${CSS.escape(prov)}"]`);
        if (provEl) addRow(provEl, model, pre);
      }
    } catch { /* ignore */ }

    // Cmd/Ctrl+Enter submits.
    form.prompt.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) { e.preventDefault(); htmx.trigger(form, "submit"); }
    });

    updateMode();
    updateCount();
  }

  // ------------------------------------------------------------ eval page

  function initEval() {
    const root = document.getElementById("eval-root");
    if (!root) return;
    const evalId = root.dataset.evalId;
    let es = null;
    let summaryTimer = null;

    const refreshSummary = () => {
      clearTimeout(summaryTimer);
      summaryTimer = setTimeout(() => {
        htmx.ajax("GET", `/evals/${evalId}/summary`, { target: "#summary", swap: "outerHTML" });
      }, 250);
    };

    const refreshCard = (runId) => {
      const card = document.getElementById(`run-${runId}`);
      const activeTab = card?.querySelector(".tab.active")?.dataset.tab;
      htmx.ajax("GET", `/evals/${evalId}/runs/${runId}`, { target: `#run-${CSS.escape(runId)}`, swap: "outerHTML" })
        .then(() => {
          // Keep the user's tab choice unless they were watching the transcript.
          if (activeTab && activeTab !== "transcript") {
            document.querySelector(`#run-${CSS.escape(runId)} .tab[data-tab="${activeTab}"]`)?.click();
          }
        });
    };

    const autoscroll = (pre, fn) => {
      const nearBottom = pre.scrollHeight - pre.scrollTop - pre.clientHeight < 40;
      fn();
      if (nearBottom) pre.scrollTop = pre.scrollHeight;
    };

    const onEvent = (ev) => {
      switch (ev.type) {
        case "run": {
          const r = ev.run;
          const card = document.getElementById(`run-${r.id}`);
          if (!card) return;
          const prev = card.dataset.status;
          if (prev === r.status) return;
          if (FINAL.has(r.status)) {
            if (!FINAL.has(prev)) { refreshCard(r.id); refreshSummary(); }
            return;
          }
          card.dataset.status = r.status;
          card.className = card.className.replace(/status-\w+/, `status-${r.status}`);
          const pill = card.querySelector(".status-pill");
          pill.textContent = r.status;
          pill.className = `pill status-${r.status} status-pill`;
          if (r.started_at) card.dataset.started = r.started_at;
          card.querySelector(".waiting")?.remove();
          refreshSummary();
          break;
        }
        case "snapshot": {
          const card = document.getElementById(`run-${ev.run_id}`);
          if (!card || FINAL.has(card.dataset.status)) return;
          const pre = card.querySelector("pre.transcript");
          if (pre) autoscroll(pre, () => setTranscript(pre, ev.transcript || ""));
          break;
        }
        case "text": {
          const pre = document.querySelector(`#run-${CSS.escape(ev.run_id)} pre.transcript`);
          if (pre) autoscroll(pre, () => appendTranscript(pre, ev.text));
          break;
        }
        case "judge":
        case "eval":
          refreshSummary();
          if (ev.type === "eval") document.getElementById("cancel-all")?.setAttribute("hidden", "");
          break;
        case "complete":
          refreshSummary();
          es?.close();
          es = null;
          break;
      }
    };

    const connect = () => {
      if (es) es.close();
      es = new EventSource(`/evals/${evalId}/stream`);
      es.onmessage = (m) => {
        try { onEvent(JSON.parse(m.data)); } catch (err) { console.error(err); }
      };
    };

    document.body.addEventListener("evalbench:reconnect", connect);

    setInterval(() => {
      const now = Date.now() / 1000;
      document.querySelectorAll('.card[data-status="running"]').forEach((card) => {
        const started = parseFloat(card.dataset.started);
        if (started) card.querySelector(".elapsed").textContent = formatElapsed(now - started);
      });
    }, 1000);

    connect();
  }

  // ------------------------------------------------------------ boot

  document.addEventListener("DOMContentLoaded", () => {
    enhance(document);
    initTheme();
    initForm();
    initEval();
    htmx.onLoad((el) => { if (el !== document.body) enhance(el); });
  });
})();
