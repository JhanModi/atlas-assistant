(() => {
  const form = document.getElementById("nps");
  if (!form) return;
  form.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const score = Number(document.getElementById("nps-score").value);
    const msg = document.getElementById("nps-msg");
    try {
      const r = await fetch("/api/nps", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ score }) });
      msg.textContent = r.ok ? "Saved." : "Couldn't save.";
      if (r.ok) setTimeout(() => location.reload(), 600);
    } catch (e) { msg.textContent = "Couldn't save."; }
  });
})();
