// Shared between the admin pages (web/admin.html, web/admin-anomalies.html)
// so login/session handling is written once, not copy-pasted per page.
// Each page still owns its own HTML (#login/#app/#login-email/etc ids
// must exist) and calls initAdminAuth(onReady) once, passing whatever
// it needs to do after a session is confirmed.

const SUPABASE_URL = "https://ybztptqixzyqgojuqtab.supabase.co";
// Safe to be public - it has no access without a matching RLS policy,
// which exists only for one authenticated admin user (see
// db/migrations/004_row_level_security.sql and later).
const SUPABASE_ANON_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InlienRwdHFpeHp5cWdvanVxdGFiIiwicm9sZSI6ImFub24iLCJpYXQiOjE3ODc4NTg4NjQsImV4cCI6MjEwMzQzNDg2NH0.yRgXKOZa-LAmyTyGKQE4xflxMsSxTA2neZwdaWojavQ";
const sb = supabase.createClient(SUPABASE_URL, SUPABASE_ANON_KEY);

let _onAuthReady = null;

async function doLogin() {
  const email = document.getElementById("login-email").value.trim();
  const password = document.getElementById("login-password").value;
  const { error } = await sb.auth.signInWithPassword({ email, password });
  if (error) {
    document.getElementById("login-status").textContent = error.message;
    return;
  }
  await _revealApp();
}

async function doLogout() {
  await sb.auth.signOut();
  location.reload();
}

async function _revealApp() {
  const { data: { session } } = await sb.auth.getSession();
  if (!session) return;
  document.getElementById("login").style.display = "none";
  document.getElementById("app").style.display = "block";
  const who = document.getElementById("who");
  if (who) who.textContent = session.user.email;
  if (_onAuthReady) _onAuthReady(session);
}

// Call once per page, after all of that page's own functions are
// defined - checks for an existing session (so a reload doesn't force
// re-login) and reveals #app once confirmed. `onReady` runs exactly
// once, right after #app becomes visible.
function initAdminAuth(onReady) {
  _onAuthReady = onReady;
  sb.auth.getSession().then(({ data: { session } }) => {
    if (session) _revealApp();
  });
}
