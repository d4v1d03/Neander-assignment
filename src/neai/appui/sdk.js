// neai's helper for app UIs. Load it with <script src="/_neai/sdk.js"></script>; it defines
// window.neai. It talks only to the app's own server, so a UI never builds URLs or handles
// Authorization headers itself.
//
//   await neai.contract()                 the app's public contract: handlers, args, access, workflows
//   await neai.me()                       {username, roles} of whoever is logged in, or null
//   await neai.signup(username, password) create an account and log in; returns the user
//   await neai.login(username, password)  returns the user
//   await neai.logout()
//   await neai.call("create_note", {...}) a handler's result; failures throw neai.NeaiError
//   neai.onAuthChange(fn)                 fn() runs after login, logout or an expired login
(function () {
  "use strict";

  // localStorage belongs to one origin, and every app server has its own (its own port), so
  // one app's UI can't read another app's login.
  const KEY = "neai_token";
  let memory = null;
  const storage = {
    get() {
      try { return localStorage.getItem(KEY); } catch (e) { return memory; }
    },
    set(token) {
      memory = token;
      try {
        if (token) localStorage.setItem(KEY, token); else localStorage.removeItem(KEY);
      } catch (e) { /* private mode: keep it in memory */ }
    },
  };
  const listeners = [];
  let contract = null;

  class NeaiError extends Error {
    constructor(error, status) {
      super((error && error.message) || "request failed");
      this.name = "NeaiError";
      this.code = error && error.code;
      this.hint = error && error.hint;
      this.details = error && error.details;
      this.status = status;
    }
  }

  function setToken(token) {
    const changed = (storage.get() || null) !== (token || null);
    storage.set(token || null);
    if (changed) listeners.forEach((fn) => { try { fn(); } catch (e) { console.error(e); } });
  }

  async function request(method, path, body) {
    const headers = { "Content-Type": "application/json" };
    const token = storage.get();
    if (token) headers.Authorization = "Bearer " + token;
    let res;
    try {
      res = await fetch(path, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) });
    } catch (e) {
      throw new NeaiError({ code: "SERVER_UNREACHABLE", message: "can't reach the app's server: " + e.message }, 0);
    }
    let data;
    try {
      data = await res.json();
    } catch (e) {
      throw new NeaiError({ code: "BAD_RESPONSE", message: `the server answered HTTP ${res.status} without JSON` }, res.status);
    }
    if (!data.ok) {
      if (data.error && data.error.code === "AUTH_INVALID") setToken(null);  // expired or logged out elsewhere
      throw new NeaiError(data.error, res.status);
    }
    return data;
  }

  window.neai = {
    NeaiError,
    contract() {
      contract = contract || request("GET", "/_neai/contract");
      return contract;
    },
    async me() {
      if (!storage.get()) return null;
      try {
        return (await request("GET", "/auth/me")).user;
      } catch (e) {
        if (e.code === "AUTH_INVALID") return null;
        throw e;
      }
    },
    async signup(username, password) {
      const r = await request("POST", "/auth/signup", { username, password });
      setToken(r.token);
      return r.user;
    },
    async login(username, password) {
      const r = await request("POST", "/auth/login", { username, password });
      setToken(r.token);
      return r.user;
    },
    async logout() {
      try {
        if (storage.get()) await request("POST", "/auth/logout", {});
      } catch (e) { /* already invalid: nothing to end */ }
      setToken(null);
    },
    async call(handler, args) {
      return (await request("POST", "/call/" + encodeURIComponent(handler), { args: args || {} })).result;
    },
    isLoggedIn() {
      return Boolean(storage.get());
    },
    onAuthChange(fn) {
      listeners.push(fn);
    },
  };
})();
