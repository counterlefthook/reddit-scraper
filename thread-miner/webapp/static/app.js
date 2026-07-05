(function () {
  var card = document.getElementById("progressCard");
  if (!card) return;
  var jobId = card.dataset.job;
  var bar = document.getElementById("bar");
  var statusLine = document.getElementById("statusLine");
  var errorLine = document.getElementById("errorLine");
  var feed = document.getElementById("feed");
  var doneActions = document.getElementById("doneActions");
  var reportCard = document.getElementById("reportCard");
  var reportFrame = document.getElementById("reportFrame");
  var timer = null;

  function showDone() {
    doneActions.hidden = false;
    reportCard.hidden = false;
    if (!reportFrame.src) reportFrame.src = "/jobs/" + jobId + "/report";
  }

  function tick() {
    fetch("/api/jobs/" + jobId + "/progress", { credentials: "same-origin" })
      .then(function (r) {
        if (r.status === 401) { window.location = "/login"; throw new Error(); }
        return r.json();
      })
      .then(function (s) {
        bar.style.width = (s.pct || 0) + "%";
        statusLine.textContent =
          s.status === "queued" ? "Waiting in the queue - it will start automatically."
          : s.status === "failed" ? "This run failed."
          : s.detail + (s.status === "running" ? "…" : "");
        errorLine.hidden = !s.error;
        if (s.error) errorLine.textContent = s.error;
        feed.innerHTML = "";
        (s.events || []).forEach(function (e) {
          var li = document.createElement("li");
          li.textContent = e.at.replace("T", " ").replace("Z", "") + "  " +
            e.event + (e.post_fullname ? "  " + e.post_fullname : "");
          feed.appendChild(li);
        });
        if (s.report_ready) { showDone(); clearInterval(timer); }
        if (s.status === "failed") clearInterval(timer);
      })
      .catch(function () { /* transient network blip: keep polling */ });
  }

  tick();
  timer = setInterval(tick, 3000);
})();
