(function () {
  const el = document.getElementById("jst-clock");
  if (!el) return;

  function formatJst(date) {
    return new Intl.DateTimeFormat("sv-SE", {
      timeZone: "Asia/Tokyo",
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hour12: false,
    }).format(date);
  }

  function tick() {
    try {
      const now = new Date();
      const text = formatJst(now);
      el.textContent = text;
      el.setAttribute("datetime", text.replace(" ", "T"));
    } catch (e) {
      el.textContent = "表示エラー";
      console.error("jst-clock:", e);
    }
  }

  tick();
  setInterval(tick, 1000);
})();
