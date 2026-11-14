const $ = (id) => document.getElementById(id);
let snapshot = null,
  busy = false;
const esc = (s) =>
  String(s).replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        c
      ],
  );
const key = () => `request-${crypto.randomUUID().slice(0, 18)}`;
async function api(path, body) {
  const response = await fetch(
    path,
    body === undefined
      ? {}
      : {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        },
  );
  const result = await response.json();
  if (!response.ok) {
    const error = new Error(result.error || "Request failed.");
    error.details = result.details;
    throw error;
  }
  return result;
}
async function action(fn) {
  if (busy) return;
  busy = true;
  for (const element of document.querySelectorAll("button,select"))
    element.disabled = true;
  try {
    await fn();
  } catch (error) {
    $("message").textContent = error.message;
    if (error.details) check(error.details);
  } finally {
    busy = false;
    for (const element of document.querySelectorAll("button,select"))
      element.disabled = false;
    $("export").disabled = !snapshot;
    $("booking-open").disabled = !snapshot;
    $("booking-status").textContent = $("message").textContent;
  }
}
function clearCheck() {
  $("check-title").textContent = "Not checked";
  $("check-message").textContent =
    "Check a proposed interval to see its minimum available capacity and any conflicting segments.";
  $("conflicts").innerHTML = "";
}
async function list(selected) {
  const data = await api("/api/resources");
  $("resource").innerHTML = data.resources
    .map(
      (r) =>
        `<option value="${esc(r.name)}">${esc(r.name)} · ${r.capacity} units</option>`,
    )
    .join("");
  if (selected && data.resources.some((r) => r.name === selected))
    $("resource").value = selected;
  await load();
}
async function load() {
  const selected = $("resource").value;
  snapshot = null;
  $("workspace").hidden = true;
  $("export").disabled = true;
  snapshot = selected ? await api(`/api/resources/${selected}`) : null;
  $("workspace").hidden = !snapshot;
  $("export").disabled = !snapshot;
  clearCheck();
  if (snapshot) render();
}
function render() {
  $("capacity").textContent = snapshot.resource.capacity;
  $("peak").textContent = snapshot.peak;
  $("total").textContent = snapshot.unit_minutes.toLocaleString();
  $("integrity").textContent = snapshot.capacity_ok
    ? "CAPACITY INVARIANT HOLDS"
    : "CAPACITY VIOLATION DETECTED";
  const filter = $("filter").value,
    bookings = snapshot.bookings.filter(
      (b) => filter === "all" || b.status === filter,
    );
  $("rows").innerHTML =
    bookings
      .map(
        (b) =>
          `<tr class="${b.status}"><td>#${b.id}<small>${esc(b.request_key)}</small></td><td>[${b.start}, ${b.end})</td><td>${b.units}</td><td>${esc(b.note)}</td><td>${b.status}<small>version ${b.version}</small></td><td>${b.status === "active" ? `<button data-cancel="${b.id}" data-version="${b.version}">Cancel</button>` : "—"}</td></tr>`,
      )
      .join("") ||
    '<tr><td colspan="6">No reservations match this view.</td></tr>';
  const segments = snapshot.segments,
    first = segments[0]?.start ?? 0,
    last = segments.at(-1)?.end ?? 240,
    span = last - first || 1,
    X = (x) => 60 + ((x - first) / span) * 900,
    Y = (u) => 205 - (u / snapshot.resource.capacity) * 150;
  let svg = `<line class="limit" x1="60" y1="55" x2="960" y2="55"/><text x="8" y="60">${snapshot.resource.capacity}</text><text x="8" y="210">0</text><text x="60" y="238">${first}</text><text x="900" y="238">${last} min</text>`;
  svg += segments
    .map(
      (s) =>
        `<rect class="area" x="${X(s.start)}" y="${Y(s.used)}" width="${Math.max(0, X(s.end) - X(s.start))}" height="${205 - Y(s.used)}"><title>[${s.start}, ${s.end}): ${s.used} of ${snapshot.resource.capacity} units, bookings ${s.booking_ids.join(", ") || "none"}</title></rect>`,
    )
    .join("");
  if (!segments.length)
    svg += '<text x="340" y="140">No active reservations</text>';
  $("plot").innerHTML = svg;
  drawLanes(first, last);
}
function payload() {
  return {
    request_key: $("request-key").value,
    start: Number($("start").value),
    end: Number($("end").value),
    units: Number($("units").value),
    note: $("note").value,
  };
}
function check(result) {
  $("check-title").textContent = result.fits
    ? "This interval fits."
    : "Insufficient capacity";
  $("check-message").textContent =
    `Minimum available: ${result.available_min} of ${result.capacity} units. ${result.fits ? "This preview is advisory; reserve to commit." : "Conflicting segments are listed below."}`;
  $("conflicts").innerHTML = result.conflicts
    .map(
      (s) =>
        `<div class="conflict">[${s.start}, ${s.end}) · ${s.used} used + ${s.requested} requested<br>Exceeds capacity by ${s.excess}. Bookings: ${s.booking_ids.map((id) => "#" + id).join(", ") || "none"}</div>`,
    )
    .join("");
}
$("booking").onsubmit = (e) => {
  e.preventDefault();
  const mode = e.submitter?.value || "preview";
  action(async () => {
    const resource = $("resource").value,
      body = payload();
    const result = await api(`/api/resources/${resource}/${mode}`, body);
    if (mode === "preview") {
      check(result);
      $("message").textContent =
        "Capacity preview completed. Nothing was reserved.";
    } else {
      await load();
      $("check-title").textContent = result.replayed
        ? "Existing request returned."
        : "Reservation committed.";
      $("check-message").textContent =
        `Booking #${result.booking.id}: ${result.booking.status}, version ${result.booking.version}. ${result.replayed ? "No new capacity was consumed." : "Capacity was checked and reserved in one transaction."}`;
      $("message").textContent = result.replayed
        ? "Retry recognized; returned current booking state."
        : "Reservation saved.";
    }
  });
};
$("booking").oninput = () => {
  clearCheck();
  $("message").textContent = "";
};
$("new-key").onclick = () => {
  $("request-key").value = key();
  clearCheck();
};
$("filter").onchange = render;
$("resource").onchange = () => action(load);
$("refresh").onclick = () =>
  action(async () => {
    await list($("resource").value);
    $("message").textContent = "Ledger refreshed from SQLite.";
  });
$("rows").onclick = (e) => {
  const button = e.target.closest("[data-cancel]");
  if (!button) return;
  action(async () => {
    await api(
      `/api/resources/${$("resource").value}/cancel/${button.dataset.cancel}`,
      { expected_version: Number(button.dataset.version) },
    );
    await load();
    $("message").textContent =
      `Booking #${button.dataset.cancel} cancelled. Capacity is available again.`;
  });
};
$("create").onsubmit = (e) => {
  e.preventDefault();
  action(async () => {
    const resource = await api("/api/resources", {
      name: $("name").value,
      capacity: Number($("new-capacity").value),
    });
    await list(resource.name);
    $("message").textContent = "Resource created with immutable capacity.";
  });
};
$("demo").onclick = () =>
  action(async () => {
    const name = "compute-" + Date.now();
    await api("/api/resources", { name, capacity: 4 });
    for (const body of [
      {
        request_key: "training-a",
        start: 0,
        end: 120,
        units: 2,
        note: "Model training A",
      },
      {
        request_key: "training-b",
        start: 60,
        end: 180,
        units: 2,
        note: "Model training B",
      },
      {
        request_key: "batch-c",
        start: 180,
        end: 240,
        units: 3,
        note: "Batch inference",
      },
    ])
      await api(`/api/resources/${name}/reserve`, body);
    await list(name);
    $("message").textContent =
      "Example resource created with three persistent reservations.";
  });
$("export").onclick = () => {
  if (!snapshot) return;
  const url = URL.createObjectURL(
      new Blob(
        [
          JSON.stringify(
            { format: "slot-ledger-export-v1", ...snapshot },
            null,
            2,
          ),
        ],
        { type: "application/json" },
      ),
    ),
    a = document.createElement("a");
  a.href = url;
  a.download = `${snapshot.resource.name}-ledger.json`;
  a.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
};
$("request-key").value = key();
action(() => list());

$("booking-open").onclick = () => $("booking-dialog").showModal();
$("booking-close").onclick = () => $("booking-dialog").close();
function drawLanes(first, last) {
  const active = snapshot.bookings.filter((b) => b.status === "active"),
    span = last - first || 1;
  const labels = Array.from(
    { length: 5 },
    (_, i) => `<span>${Math.round(first + (span * i) / 4)}</span>`,
  ).join("");
  $("lanes").innerHTML =
    `<div class="lane-axis"><span>Booking / units</span><div>${labels}</div></div>` +
    active
      .map(
        (b, i) =>
          `<div class="lane"><div class="lane-label"><strong>${esc(b.note || b.request_key)}</strong><span>#${b.id} · ${b.units} units</span></div><div class="lane-track"><div class="reservation-bar tone-${i % 4}"><span>[${b.start}, ${b.end})</span></div></div></div>`,
      )
      .join("");
  $("lanes")
    .querySelectorAll(".reservation-bar")
    .forEach((bar, i) => {
      const b = active[i];
      bar.style.left = `${((b.start - first) / span) * 100}%`;
      bar.style.width = `${((b.end - b.start) / span) * 100}%`;
      bar.title = `[${b.start}, ${b.end}) · ${b.units} units · ${b.note || b.request_key}`;
    });
  if (!active.length)
    $("lanes").innerHTML += '<p class="empty-lanes">No active reservations</p>';
}
