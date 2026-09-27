"""The page an operator actually dials from.

``/api/v1/calls/next`` returns JSON. Nobody rings a practice from JSON, and on
Tuesday morning the difference between "the calling system exists" and "twenty
calls happened" is a screen with the number on it, the sentence to open with,
and somewhere to put what came back.

**Served from the API rather than the CRM or a published page, for one hard
reason:** the browser has to call these endpoints, and they sit behind a bearer
token on this origin. A page hosted anywhere else is a CORS problem and a
second place to keep a secret. Same origin, no preflight, no new deployment
target.

**The token is the operator's, and it stays in their browser.** Asked for once,
kept in ``localStorage``, sent as ``Authorization``. It is deliberately not
baked into this file: the file lives in the repository and the token does not.

**Everything the page shows comes from ``/briefing``.** No claim is composed
here. That is the same rule the voice agent works under -- the evidence API
refuses stale, suppressed and already-called leads, and a screen that made up
its own sentences would route around the one gate that keeps a cold call
truthful.
"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

router = APIRouter(prefix="/api/v1/calls", tags=["calls"])


#: Deliberately one file with no build step and no dependencies.
#:
#: It ships inside the API image, so it is current whenever the API is, and
#: there is no second thing to deploy at the moment somebody wants to make
#: twenty calls.
_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>Call sheet</title>
<style>
  :root {
    color-scheme: light dark;
    --paper:#f1f2f4; --card:#fff; --sunk:#e8eaee; --ink:#12161c;
    --muted:#5c6672; --rule:#d4d9e0; --accent:#1f3352;
    --bad:#9c3328; --bad-bg:#f6e6e3; --good:#2c6248; --good-bg:#e2efe8;
    --warn:#8c6015; --warn-bg:#f7eeda;
    --mono:ui-monospace,"SF Mono",Menlo,Consolas,monospace;
  }
  @media (prefers-color-scheme: dark) {
    :root { --paper:#0f1216; --card:#161b22; --sunk:#1b212a; --ink:#e5e9ee;
            --muted:#99a3b0; --rule:#2a323c; --accent:#93b3e2;
            --bad:#e58374; --bad-bg:#2d1c1a; --good:#74c69a; --good-bg:#16261f;
            --warn:#d9a441; --warn-bg:#2b2317; }
  }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--paper); color:var(--ink); font:16px/1.55
         system-ui,-apple-system,"Segoe UI",sans-serif;
         padding:0 16px env(safe-area-inset-bottom,0px); }
  .wrap { max-width:840px; margin:0 auto; padding-block:20px 60px; }
  h1 { font-size:1.2rem; margin:0; letter-spacing:-.01em; }
  .bar { display:flex; align-items:center; gap:12px; flex-wrap:wrap;
         border-bottom:2px solid var(--ink); padding-bottom:12px; margin-bottom:18px; }
  .bar .sp { margin-left:auto; }
  .queue { font:600 12px/1 var(--mono); color:var(--muted); letter-spacing:.08em;
           text-transform:uppercase; }
  button { font:inherit; padding:9px 15px; border:1px solid var(--rule);
           background:var(--card); color:var(--ink); border-radius:3px; cursor:pointer; }
  button:hover { border-color:var(--accent); }
  button.primary { background:var(--accent); color:var(--card); border-color:var(--accent); }
  button:disabled { opacity:.45; cursor:default; }
  select, input, textarea { font:inherit; padding:8px 10px; border:1px solid var(--rule);
           border-radius:3px; background:var(--card); color:var(--ink); width:100%; }
  .card { background:var(--card); border:1px solid var(--rule); padding:18px 20px;
          margin-bottom:14px; }
  .practice { font-size:1.55rem; font-weight:600; line-height:1.15; margin:0 0 4px;
              letter-spacing:-.015em; }
  .meta { font:12.5px/1.5 var(--mono); color:var(--muted); margin-bottom:14px;
          display:flex; flex-wrap:wrap; gap:4px 14px; }
  .phone { font:600 1.9rem/1.1 var(--mono); letter-spacing:.01em; margin:2px 0 3px;
           user-select:all; word-break:break-all; }
  .opener { font-size:1.08rem; line-height:1.5; background:var(--sunk);
            border-left:3px solid var(--accent); padding:13px 16px; margin:14px 0; }
  .ask { font-size:1.02rem; line-height:1.5; border-left:3px solid var(--good);
         background:var(--good-bg); padding:13px 16px; margin:14px 0; }
  .lbl { font:600 10.5px/1 var(--mono); letter-spacing:.11em; text-transform:uppercase;
         color:var(--muted); display:block; margin-bottom:6px; }
  a { color:var(--accent); text-underline-offset:2px; word-break:break-all; }
  .chip { font:600 10.5px/1 var(--mono); letter-spacing:.07em; text-transform:uppercase;
          padding:4px 8px; border-radius:2px; display:inline-block; }
  .chip.t-booking_dead { background:var(--bad-bg); color:var(--bad); }
  .chip.t-conversion { background:var(--warn-bg); color:var(--warn); }
  .chip.t-automation { background:var(--sunk); color:var(--ink); }
  .chip.t-cosmetic { background:var(--sunk); color:var(--muted); }
  .chip.stale { background:var(--bad-bg); color:var(--bad); }
  .grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:10px; }
  .outcomes { display:flex; flex-wrap:wrap; gap:7px; margin-bottom:12px; }
  .outcomes button.on { background:var(--accent); color:var(--card); border-color:var(--accent); }
  .note { font-size:13px; color:var(--muted); margin:8px 0 0; }
  .warn { background:var(--warn-bg); border-left:3px solid var(--warn);
          padding:12px 15px; font-size:14px; margin-bottom:14px; }
  .danger { background:var(--bad-bg); border-left:3px solid var(--bad);
            padding:12px 15px; font-size:14px; margin-bottom:14px; }
  .proj { border-top:1px solid var(--rule); padding-top:10px; margin-top:10px; font-size:14px; }
  .proj:first-of-type { border-top:0; }
  [hidden] { display:none !important; }
  .row { display:flex; gap:10px; flex-wrap:wrap; align-items:center; }
</style>
</head>
<body>
<div class="wrap">

  <div class="bar">
    <h1>Call sheet</h1>
    <span class="queue" id="queue"></span>
    <span class="sp"></span>
    <select id="country" style="width:auto">
      <option value="">All countries</option>
      <option value="GB">GB</option><option value="US">US</option>
      <option value="CA">CA</option><option value="AU">AU</option>
      <option value="IE">IE</option><option value="AE">AE</option>
      <option value="NL">NL</option><option value="PL">PL</option>
      <option value="RO">RO</option>
    </select>
    <button id="reload">Load queue</button>
  </div>

  <div id="setup" class="card" hidden>
    <span class="lbl">Agent token</span>
    <p class="note" style="margin:0 0 10px">
      Kept in this browser only. Never sent anywhere but this server.
    </p>
    <div class="row">
      <input id="token" type="password" placeholder="TITAN_CALL_AGENT_TOKEN"
             style="flex:1;min-width:220px" autocomplete="off">
      <button class="primary" id="save">Save</button>
    </div>
  </div>

  <div id="msg" class="card" hidden></div>

  <div id="sheet" hidden>
    <div class="card">
      <p class="practice" id="practice"></p>
      <div class="meta" id="meta"></div>
      <span class="lbl">Dial</span>
      <p class="phone" id="phone"></p>
      <p class="note" id="phonenote"></p>

      <div id="stalebox" class="danger" hidden></div>

      <span class="lbl" style="margin-top:16px">Open with</span>
      <div class="opener" id="opener"></div>

      <span class="lbl">Then ask</span>
      <div class="ask" id="ask"></div>

      <div id="evidencebox">
        <span class="lbl">What they will look at</span>
        <p style="margin:0 0 4px"><a id="evidence" target="_blank" rel="noopener"></a></p>
        <p class="note" id="observed"></p>
      </div>

      <div id="projects"></div>
    </div>

    <div class="card">
      <span class="lbl">What happened</span>
      <div class="outcomes" id="outcomes"></div>

      <div id="dnc" class="danger" hidden>
        This writes a permanent do-not-call for this number, effective before
        any next dial. It cannot be undone from here.
      </div>

      <div class="grid" style="margin-bottom:12px">
        <div><span class="lbl">Who you spoke to</span>
          <input id="name" placeholder="optional" autocomplete="off"></div>
        <div><span class="lbl">Their role</span>
          <input id="role" placeholder="optional" autocomplete="off"></div>
      </div>
      <div class="grid" style="margin-bottom:12px">
        <div><span class="lbl">Email, if they offered one</span>
          <input id="email" type="email" placeholder="optional" autocomplete="off"></div>
        <div><span class="lbl">Call back at</span>
          <input id="callback" type="datetime-local"></div>
      </div>
      <div style="margin-bottom:12px">
        <span class="lbl">Notes</span>
        <textarea id="notes" rows="3" placeholder="What they actually said"></textarea>
      </div>
      <label class="row" style="margin-bottom:14px;font-size:14px">
        <input type="checkbox" id="consent" style="width:auto">
        They agreed to be emailed
      </label>

      <div class="row">
        <button class="primary" id="record" disabled>Record and next</button>
        <button id="skip">Skip, do not record</button>
      </div>
    </div>
  </div>

</div>

<script>
(function () {
  "use strict";
  var KEY = "titan.call.token";
  var token = "", queue = [], at = 0, current = null, outcome = null, busy = false;

  var $ = function (id) { return document.getElementById(id); };

  function store(k, v) { try { localStorage.setItem(k, v); } catch (e) {} }
  function load(k) { try { return localStorage.getItem(k) || ""; } catch (e) { return ""; } }

  function say(html, kind) {
    var m = $("msg");
    m.innerHTML = html;
    m.style.borderLeft = kind === "bad" ? "3px solid var(--bad)" : "3px solid var(--accent)";
    m.hidden = false;
  }
  function quiet() { $("msg").hidden = true; }

  function api(path, opts) {
    opts = opts || {};
    opts.headers = Object.assign(
      { "Authorization": "Bearer " + token }, opts.headers || {}
    );
    return fetch("/api/v1/calls" + path, opts).then(function (r) {
      if (r.status === 401) { throw new Error("The token was not accepted."); }
      if (!r.ok) {
        return r.text().then(function (t) {
          throw new Error("HTTP " + r.status + (t ? ": " + t.slice(0, 200) : ""));
        });
      }
      return r.status === 204 ? null : r.json();
    });
  }

  // ---- queue -------------------------------------------------------------
  function loadQueue() {
    if (!token) { $("setup").hidden = false; return; }
    quiet();
    $("reload").disabled = true;
    var cc = $("country").value;
    api("/next?limit=40" + (cc ? "&country=" + encodeURIComponent(cc) : ""))
      .then(function (rows) {
        queue = rows || []; at = 0;
        if (!queue.length) {
          $("sheet").hidden = true;
          say("Nothing callable" + (cc ? " in " + cc : "") +
              ". Every candidate is suppressed, already called, or its evidence " +
              "is older than fourteen days \\u2014 the gate refuses to put a stale " +
              "claim in your mouth. Re-crawl, or try another country.");
        } else {
          show();
        }
      })
      .catch(fail)
      .then(function () { $("reload").disabled = false; });
  }

  function fail(err) {
    $("sheet").hidden = true;
    say("<strong>" + esc(err.message) + "</strong>", "bad");
    if (/token/i.test(err.message)) { $("setup").hidden = false; }
  }

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  // ---- one practice ------------------------------------------------------
  function show() {
    if (at >= queue.length) {
      $("sheet").hidden = true;
      say("Queue finished \\u2014 " + queue.length + " worked through. " +
          "Load again for more.");
      return;
    }
    quiet();
    reset();
    var row = queue[at];
    $("queue").textContent = (at + 1) + " of " + queue.length;
    api("/briefing?lead_id=" + encodeURIComponent(row.lead_id))
      .then(function (b) { current = b; paint(b); $("sheet").hidden = false; })
      .catch(function (err) {
        // A lead can go uncallable between listing and dialling -- somebody
        // suppressed it, or its evidence aged past the gate. Skip rather than
        // stop: the queue is still good.
        if (/404/.test(err.message)) { at += 1; show(); return; }
        fail(err);
      });
  }

  function paint(b) {
    $("practice").textContent = b.practice;
    $("phone").textContent = b.phone;
    $("phonenote").textContent =
      "Select to copy. Dial it yourself \\u2014 nothing here places the call.";

    var bits = [];
    bits.push('<span class="chip t-' + esc(b.tier) + '">' + esc(b.tier.replace(/_/g, " ")) + "</span>");
    if (b.review_count != null) { bits.push(b.review_count + " reviews"); }
    if (b.rating != null) { bits.push(b.rating + "\\u2605"); }
    if (b.evidence_age_days != null) {
      var stale = b.evidence_age_days > 7;
      bits.push('<span class="' + (stale ? 'chip stale' : '') + '">evidence ' +
                b.evidence_age_days + "d old</span>");
    }
    if (b.website) {
      bits.push('<a href="' + esc(b.website) + '" target="_blank" rel="noopener">site</a>');
    }
    $("meta").innerHTML = bits.join("");

    var old = b.evidence_age_days != null && b.evidence_age_days > 10;
    $("stalebox").hidden = !old;
    if (old) {
      $("stalebox").textContent =
        "This was measured " + b.evidence_age_days + " days ago. Open the page " +
        "before you dial and check it is still true \\u2014 being wrong in the " +
        "first sentence of a cold call cannot be walked back.";
    }

    $("opener").textContent = b.opener;
    $("ask").textContent = b.ask;

    if (b.evidence_url) {
      $("evidencebox").hidden = false;
      $("evidence").href = b.evidence_url;
      $("evidence").textContent = b.evidence_url;
      $("observed").textContent = b.observed || "";
    } else {
      $("evidencebox").hidden = true;
    }

    var p = $("projects");
    if (b.projects && b.projects.length) {
      p.innerHTML = '<span class="lbl" style="margin-top:16px">' +
        "Your own work you may mention</span>" +
        b.projects.map(function (x) {
          return '<div class="proj"><strong>' + esc(x.name) + "</strong> \\u2014 " +
                 esc(x.summary) + '<br><a href="' + esc(x.url) +
                 '" target="_blank" rel="noopener">' + esc(x.url) + "</a></div>";
        }).join("");
    } else {
      p.innerHTML = "";
    }
  }

  // ---- outcome -----------------------------------------------------------
  var OUTCOMES = [
    ["no_answer", "No answer"], ["gatekeeper", "Gatekeeper"],
    ["reached_dm", "Reached decision maker"], ["interested", "Interested"],
    ["callback", "Call back"], ["not_interested", "Not interested"],
    ["wrong_number", "Wrong number"], ["do_not_call", "Do not call again"]
  ];

  function buildOutcomes() {
    $("outcomes").innerHTML = OUTCOMES.map(function (o) {
      return '<button type="button" data-o="' + o[0] + '">' + o[1] + "</button>";
    }).join("");
    Array.prototype.forEach.call($("outcomes").children, function (btn) {
      btn.addEventListener("click", function () {
        outcome = btn.getAttribute("data-o");
        Array.prototype.forEach.call($("outcomes").children, function (b) {
          b.className = b === btn ? "on" : "";
        });
        $("dnc").hidden = outcome !== "do_not_call";
        $("record").disabled = false;
      });
    });
  }

  function reset() {
    outcome = null;
    $("record").disabled = true;
    $("dnc").hidden = true;
    Array.prototype.forEach.call($("outcomes").children, function (b) { b.className = ""; });
    ["name", "role", "email", "callback", "notes"].forEach(function (id) { $(id).value = ""; });
    $("consent").checked = false;
  }

  function record() {
    if (!current || !outcome || busy) { return; }
    busy = true;
    $("record").disabled = true;
    var cb = $("callback").value;
    api("/outcome", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        lead_id: current.lead_id,
        phone: current.phone,
        stage: 1,
        outcome: outcome,
        contact_name: $("name").value || null,
        contact_role: $("role").value || null,
        contact_email: $("email").value || null,
        consent_to_email: $("consent").checked,
        callback_at: cb ? new Date(cb).toISOString() : null,
        notes: $("notes").value || null
      })
    }).then(function () {
      at += 1;
      show();
    }).catch(function (err) {
      fail(err);
      $("record").disabled = false;
    }).then(function () { busy = false; });
  }

  // ---- wiring ------------------------------------------------------------
  buildOutcomes();
  $("record").addEventListener("click", record);
  $("skip").addEventListener("click", function () { at += 1; show(); });
  $("reload").addEventListener("click", loadQueue);
  $("country").addEventListener("change", loadQueue);
  $("save").addEventListener("click", function () {
    token = $("token").value.trim();
    if (!token) { return; }
    store(KEY, token);
    $("setup").hidden = true;
    $("token").value = "";
    loadQueue();
  });
  $("token").addEventListener("keydown", function (e) {
    if (e.key === "Enter") { $("save").click(); }
  });

  token = load(KEY);
  if (token) { loadQueue(); } else { $("setup").hidden = false; }
})();
</script>
</body>
</html>
"""


@router.get("/sheet", response_class=HTMLResponse, include_in_schema=False)
async def call_sheet() -> HTMLResponse:
    """The dialling screen.

    Deliberately unauthenticated, because it holds nothing. Every byte of lead
    data arrives through the token-checked endpoints this page then calls; an
    operator without a token sees an empty form asking for one.

    ``include_in_schema=False`` keeps a page out of the machine-readable API
    description that describes the agent contract.
    """
    return HTMLResponse(_PAGE)


__all__ = ["router"]
