// Today's briefing, shown as Atlas's first message. Rendered with textContent only (docs/03-security.md).
// Items written by other people (e.g. someone else's invite) are labelled and never turned into links.
(() => {
  const box = document.getElementById("briefing");
  if (!box) return;

  const el = (tag, cls, text) => {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined && text !== null) n.textContent = text;
    return n;
  };
  async function call(method, url, body) {
    const r = await fetch(url, { method, headers: { "Content-Type": "application/json" },
                                 body: body ? JSON.stringify(body) : undefined });
    if (!r.ok) throw new Error(`${r.status}`);
    return r.json();
  }

  function itemLine(v) {
    const li = el("li");
    let title = el("span", "title", v.title);
    if (v.url && !v.untrusted && v.url.startsWith("https://")) {
      title = el("a", "title", v.title);
      title.href = v.url;
      title.target = "_blank";
      title.rel = "noopener noreferrer";
    }
    li.append(title);
    if (v.course && !v.title.includes(v.course)) li.append(el("span", "muted", ` · ${v.course}`));
    if (v.when) li.append(el("span", "when", ` · ${v.when}`));
    if (v.location) li.append(el("span", "muted", ` · ${v.location}`));
    if (v.status === "missing") li.append(el("span", "badge bad", "missing"));
    if (v.status === "optional") li.append(el("span", "badge", "optional"));
    if (v.untrusted) li.append(el("span", "badge", "from someone else"));
    return li;
  }

  function section(title, items, render, emptyText) {
    const s = el("section");
    s.append(el("h3", "", title));
    if (!items.length) { s.append(el("p", "muted small", emptyText)); return s; }
    const ul = el("ul");
    items.forEach((x) => ul.append(render(x)));
    s.append(ul);
    return s;
  }

  function alertsBlock(b) {
    const alerts = b.content.alerts || [];
    if (!alerts.length) return null;
    const s = el("section");
    s.append(el("h3", "", "Alerts"), el("p", "muted small", "Was each one useful? (This measures alert precision.)"));
    const ul = el("ul");
    alerts.forEach((a) => {
      const li = el("li");
      li.append(el("span", a.urgent ? "urgent" : "", a.title));
      const rated = b.alert_feedback[a.id];
      const state = el("span", "muted small", rated ? ` · ${rated === "useful" ? "useful" : "not useful"}` : "");
      if (!rated) {
        const yes = el("button", "ghost small", "👍");
        const no = el("button", "ghost small", "👎");
        const rate = async (useful) => {
          yes.disabled = no.disabled = true;
          try { await call("POST", `/api/alerts/${a.id}/feedback`, { useful }); state.textContent = useful ? " · useful" : " · not useful"; }
          catch (e) { state.textContent = " · couldn't save"; }
        };
        yes.onclick = () => rate(true);
        no.onclick = () => rate(false);
        li.append(" ", yes, no);
      }
      li.append(state);
      ul.append(li);
    });
    s.append(ul);
    return s;
  }

  function gateBlock(b) {
    const s = el("section", "gate");
    if (b.verdict) {
      s.append(el("p", "muted small", `Checked: ${b.verdict}` + (b.missed_items ? ` · ${b.missed_items} item(s) missed` : "")
                                      + (b.verdict_note ? ` · ${b.verdict_note}` : "")));
      return s;
    }
    s.append(el("p", "small", "Does this match Canvas and your calendar? (Phase 2 gate: 7 accurate days in a row.)"));
    const yes = el("button", "small", "Yes, accurate");
    const no = el("button", "ghost small", "Something's wrong");
    const form = el("div", "wrong");
    form.hidden = true;
    const missed = el("input");
    missed.type = "number"; missed.min = "0"; missed.max = "100"; missed.value = "1";
    const note = el("input");
    note.maxLength = 500; note.placeholder = "What was missing or wrong?";
    const save = el("button", "small", "Save");
    const msg = el("span", "muted small");
    form.append(el("label", "small", "Items missed or wrong: "), missed, note, save);
    const send = async (verdict) => {
      yes.disabled = no.disabled = save.disabled = true;
      try {
        await call("POST", `/api/briefing/${b.id}/verdict`, verdict === "accurate" ? { verdict }
          : { verdict, missed: Number(missed.value || 0), note: note.value || null });
        load();
      } catch (e) { msg.textContent = "Couldn't save."; yes.disabled = no.disabled = save.disabled = false; }
    };
    yes.onclick = () => send("accurate");
    no.onclick = () => { form.hidden = false; };
    save.onclick = () => send("inaccurate");
    s.append(yes, no, form, msg);
    return s;
  }

  function render(b) {
    box.replaceChildren();
    const c = b.content;
    const head = el("div", "head");
    head.append(el("strong", "", `Today's briefing · ${c.heading}`));
    const refresh = el("button", "ghost small", "Refresh");
    refresh.title = "Rebuild from the latest synced data";
    refresh.onclick = async () => { refresh.disabled = true; try { render((await call("POST", "/api/briefing")).briefing); } catch (e) { refresh.disabled = false; } };
    head.append(refresh);
    box.append(head, el("p", "summary", c.summary));
    const meta = b.daily ? (b.trigger === "catch_up" ? "caught up: the scheduled run was missed (Atlas or the laptop was off)"
                                                     : "delivered on schedule") : "made on request";
    box.append(el("p", "muted small", `Generated ${c.generated} · ${meta}`));

    const stale = c.health.filter((h) => h.stale);
    if (stale.length || c.caveats.length) {
      const warn = el("div", "banner");
      stale.forEach((h) => warn.append(el("div", "", `${h.source}: ${h.message}` + (h.last_success ? ` (data from ${h.last_success})` : ""))));
      c.caveats.forEach((x) => warn.append(el("div", "", x)));
      box.append(warn);
    }
    const details = el("details");
    details.open = true;
    details.append(el("summary", "small muted", "Details"));
    if (c.missing.length) details.append(section("Missing submissions", c.missing, itemLine, ""));
    details.append(section("Today", c.today, itemLine, "Nothing scheduled."));
    details.append(section("Due in the next 48 hours", c.due_48h, itemLine, "Nothing due."));
    details.append(section("Due later this week", c.due_week, itemLine, "Nothing else this week."));
    details.append(section("Clashes (next 14 days)", c.clashes, (x) => {
      const li = el("li");
      li.append(el("span", "title", `${x.a.untrusted ? "An invite" : x.a.title} overlaps ${x.b.untrusted ? "an invite" : x.b.title}`),
                el("span", "when", ` · ${x.minutes} min · ${x.a.when}`));
      if (x.optional) li.append(el("span", "badge", "optional"));
      return li;
    }, "No clashes."));
    if (c.announcements.length) details.append(section("New announcements", c.announcements, itemLine, ""));
    const ab = alertsBlock(b);
    if (ab) details.append(ab);
    box.append(details);
    if (b.daily) box.append(gateBlock(b));
    box.hidden = false;
  }

  async function load() {
    try {
      const { briefing } = await call("GET", "/api/briefing");
      if (briefing) render(briefing);
    } catch (e) { /* no briefing yet: the chat still works */ }
  }
  load();
})();
