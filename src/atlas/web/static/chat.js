// Atlas chat. Model output is always rendered with textContent (never innerHTML): see docs/03-security.md.
(() => {
  const log = document.getElementById("log");
  const form = document.getElementById("ask");
  const input = document.getElementById("text");
  const send = document.getElementById("send");
  const banner = document.getElementById("banner");
  let sessionId = sessionStorage.getItem("atlas-session") || crypto.randomUUID().replace(/-/g, "");
  sessionStorage.setItem("atlas-session", sessionId);

  const el = (tag, cls, text) => {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined) n.textContent = text;
    return n;
  };
  const fmtMs = (v) => (v == null ? "—" : v < 1000 ? `${Math.round(v)} ms` : `${(v / 1000).toFixed(1)} s`);
  const scroll = () => { log.scrollTop = log.scrollHeight; };

  async function post(url, body) {
    const r = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    if (!r.ok) throw new Error(`${r.status} ${await r.text()}`);
    return r.json();
  }

  function feedbackRow(interactionId) {
    const row = el("div", "feedback");
    const up = el("button", "ghost small", "👍");
    const down = el("button", "ghost small", "👎");
    const reason = el("select", "small");
    [["", "Why? (optional)"], ["misunderstood", "Misunderstood me"], ["wrong_data", "Wrong / missing info"],
     ["too_slow", "Too slow"], ["other", "Other"]].forEach(([v, t]) => { const o = el("option", "", t); o.value = v; reason.append(o); });
    reason.hidden = true;
    const done = el("span", "muted small");
    const sendFb = async (thumbs) => {
      try {
        await post("/api/feedback", { interaction_id: interactionId, thumbs, reason: reason.value || null });
        done.textContent = "Thanks: recorded.";
      } catch (e) { done.textContent = "Couldn't save feedback."; }
    };
    up.onclick = () => { up.disabled = down.disabled = true; sendFb(1); };
    down.onclick = () => { reason.hidden = false; up.disabled = down.disabled = true; done.textContent = ""; sendFb(-1); };
    reason.onchange = () => sendFb(-1);
    row.append(up, down, reason, done);
    return row;
  }

  function renderResult(res, bubble) {
    bubble.replaceChildren();
    bubble.append(el("div", "text", res.reply || "(no answer)"));
    if (res.approvals && res.approvals.length) {
      res.approvals.forEach((a) => bubble.append(approvalCard(a, bubble)));
    }
    if (res.status !== "awaiting_approval") {
      const m = res.metrics || {};
      const meta = el("div", "meta muted",
        `${res.status} · ${fmtMs(m.e2e_ms)} total · model ${fmtMs(m.model_ms)} · tools ${fmtMs(m.tool_ms)}` +
        (res.tools && res.tools.length ? ` · ${res.tools.join(", ")}` : ""));
      bubble.append(meta, feedbackRow(res.interaction_id));
    }
    scroll();
  }

  function approvalCard(a, bubble) {
    const card = el("div", "approval");
    card.append(el("div", "", `Approve "${a.tool}"?`), el("pre", "", JSON.stringify(a.args, null, 2)), el("div", "muted small", a.why || ""));
    const yes = el("button", "", "Approve");
    const no = el("button", "ghost", "Reject");
    const decide = async (approve) => {
      yes.disabled = no.disabled = true;
      try {
        const res = await post(`/api/approvals/${a.id}`, { approve });
        if (res.interaction_id) renderResult(res, bubble);
      } catch (e) { card.append(el("div", "error", String(e))); }
    };
    yes.onclick = () => decide(true);
    no.onclick = () => decide(false);
    card.append(yes, no);
    return card;
  }

  async function ask(text) {
    log.append(el("div", "msg you", text));
    const bubble = el("div", "msg atlas");
    bubble.append(el("div", "muted thinking", "Thinking…"));
    log.append(bubble);
    scroll();
    send.disabled = true;
    try {
      renderResult(await post("/api/chat", { text, session_id: sessionId }), bubble);
    } catch (e) {
      bubble.replaceChildren(el("div", "error", `Request failed: ${e.message}`));
    } finally {
      send.disabled = false;
      input.focus();
    }
  }

  form.addEventListener("submit", (ev) => {
    ev.preventDefault();
    const text = input.value.trim();
    if (!text) return;
    input.value = "";
    ask(text);
  });
  document.querySelectorAll(".chip").forEach((c) => c.addEventListener("click", () => ask(c.textContent)));
  document.getElementById("reset").onclick = async () => {
    await post(`/api/session/${sessionId}/reset`, {}).catch(() => {});
    sessionId = crypto.randomUUID().replace(/-/g, "");
    sessionStorage.setItem("atlas-session", sessionId);
    log.querySelectorAll(".msg:not(.intro)").forEach((n) => n.remove());
  };

  fetch("/api/health").then((r) => r.json()).then((h) => {
    const status = document.getElementById("model-status");
    if (!h.ollama.reachable) {
      banner.textContent = "Ollama isn't running at the configured address. Install/start Ollama, then reload. (Run `atlas doctor` for details.)";
      banner.hidden = false;
      status.classList.add("bad");
    } else if (!h.ollama.model_ready) {
      banner.textContent = `Model ${h.model_id} isn't pulled yet. Run: ollama pull ${h.model_id}`;
      banner.hidden = false;
      status.classList.add("bad");
    } else {
      status.classList.add("ok");
    }
  }).catch(() => {});
})();
