// Placeholder UI: shows the store status and the revision. All URLs are relative (Ingress prefix).
const out = document.getElementById("status");

try {
  const res = await fetch("api/session", { headers: { Accept: "application/json" } });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  const session = await res.json();
  const problem = session.store.problem ? ` (${session.store.problem})` : "";
  out.textContent = `Store: ${session.store.status}${problem}, revision ${session.revision}`;
} catch (err) {
  out.textContent = `Could not load the session: ${err.message}`;
}
