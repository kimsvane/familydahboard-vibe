// Tester Aula-sidens rene filtre og rendering. app.js kører i en minimal
// DOM-strump med en selector-oversigt, så vi kan sætte værdier i
// filterfelterne og se, hvilke rækker der kommer med.
const fs = require("fs");
const path = require("path");
const vm = require("vm");
const assert = require("assert");

const source = fs.readFileSync(path.join(__dirname, "..", "web", "app.js"), "utf8");

function lavElement(egenskaber = {}) {
  return {
    hidden: false,
    textContent: "",
    innerHTML: "",
    value: "",
    checked: false,
    disabled: false,
    classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
    setAttribute() {},
    getAttribute() { return null; },
    removeAttribute() {},
    addEventListener() {},
    append() {},
    remove() {},
    querySelector() { return lavElement(); },
    querySelectorAll() { return []; },
    appendChild() {},
    getBoundingClientRect() { return { height: 0, width: 0 }; },
    focus() {},
    dataset: {},
    style: {},
    className: "",
    options: [],
    ...egenskaber,
  };
}

const felter = {
  "#aula-search": lavElement(),
  "#aula-state-filter": lavElement(),
  "#aula-child-filter": lavElement(),
  "#aula-status": lavElement(),
  "#aula-count": lavElement(),
  "#aula-setup": lavElement(),
  "#aula-code-fields": lavElement(),
  "#aula-login-hint": lavElement(),
  "#aula-login-error": lavElement(),
  "#aula-thread-list": lavElement(),
  "#aula-post-list": lavElement(),
  "#aula-calendar-list": lavElement(),
  "#aula-calendar-fallback-note": lavElement(),
};

const noopElement = lavElement();

const context = {
  document: {
    documentElement: { style: { setProperty() {} } },
    body: { classList: { toggle() {} }, dataset: {} },
    cookie: "",
    querySelector: (vælger) => felter[vælger] || noopElement,
    querySelectorAll: () => [],
    getElementById: () => noopElement,
    createElement: () => lavElement(),
    addEventListener() {},
  },
  localStorage: { getItem: () => null, setItem() {} },
  sessionStorage: { getItem: () => null, setItem() {}, removeItem() {} },
  navigator: {},
  window: {
    addEventListener() {},
    scrollTo() {},
    setTimeout: (fn) => { fn(); return 1; },
    clearTimeout() {},
    setInterval: () => 1,
    clearInterval() {},
  },
  location: { origin: "http://test", reload() {} },
  setTimeout: (fn) => { fn(); return 1; },
  setInterval() { return { unref() {} }; },
  clearTimeout() {},
  clearInterval() {},
  Intl,
  Date,
  Math,
  JSON,
  console,
  fetch: async () => { throw new Error("ingen netværk i testen"); },
};
context.window.document = context.document;
context.globalThis = context;
vm.createContext(context);
vm.runInContext(source, context, { filename: "app.js" });

const { aulaMatchesChild, aulaMatchesState, aulaMatchesSearch, aulaFilter,
  aulaStatusText, aulaFallbackEvent, escapeHtml } = context;

const børn = [
  { profile_id: "111", name: "Emil", institution: "Nord" },
  { profile_id: "222", name: "Mia", institution: "Nord" },
];

const tråd = {
  thread_id: "t1",
  subject: "Hej med dig",
  sender: "Lærer",
  participants: ["Emil"],
  is_unread: true,
  starred: false,
};

const opslag = {
  post_id: "p1",
  title: "Ture i morgen",
  author: "Lærer",
  body: "Medtag regntøj",
  audience: ["3.a"],
  is_unread: false,
  starred: true,
};

/* state er en const i app.js og lander derfor ikke som egenskab på det
   globale objekt. Vi sætter den med et nyt udtryk i samme kontekst,
   som deler app.js' leksikalske område. */
const sætIState = (udtryk, værdi) => vm.runInContext(`${udtryk} = ${JSON.stringify(værdi)}`, context);

function sætFilters({ søg = "", tilstand = "alle", barn = "alle" } = {}) {
  felter["#aula-search"].value = søg;
  felter["#aula-state-filter"].value = tilstand;
  felter["#aula-child-filter"].value = barn;
}

function filtrér(rækker, filtre = {}) {
  sætFilters(filtre);
  sætIState("state.aula", { children: børn, configured: true });
  return aulaFilter(rækker);
}

let bestået = 0;
const fejlene = [];
const tjek = (navn, kør) => {
  try {
    kør();
    bestået += 1;
    process.stdout.write(`  ok  ${navn}\n`);
  } catch (fejl) {
    fejlene.push(fejl);
    process.stdout.write(`  FEJL  ${navn}\n`);
  }
};

tjek("filterfunktionerne findes", () => {
  assert.equal(typeof aulaMatchesChild, "function");
  assert.equal(typeof aulaMatchesState, "function");
  assert.equal(typeof aulaMatchesSearch, "function");
  assert.equal(typeof aulaFilter, "function");
});

tjek("uden filtre kommer alt med", () => {
  assert.equal(filtrér([tråd, opslag]).length, 2);
});

tjek("søgning rammer emne, afsender og indhold", () => {
  assert.equal(filtrér([tråd, opslag], { søg: "ture" }).length, 1);
  assert.equal(filtrér([tråd, opslag], { søg: "lærer" }).length, 2);
  assert.equal(filtrér([tråd, opslag], { søg: "regntøj" }).length, 1);
  assert.equal(filtrér([tråd, opslag], { søg: "ingenting" }).length, 0);
});

tjek("søgning ser store og små bogstaver ens", () => {
  assert.equal(filtrér([tråd], { søg: "HEJ" }).length, 1);
});

tjek("ulæst-filteret bruger aulas egen markering", () => {
  assert.deepEqual(filtrér([tråd, opslag], { tilstand: "ulast" }), [tråd]);
  assert.deepEqual(filtrér([tråd, opslag], { tilstand: "lest" }), [opslag]);
});

tjek("stjerne-filteret bruger den lokale stjerne", () => {
  assert.deepEqual(filtrér([tråd, opslag], { tilstand: "stjernet" }), [opslag]);
});

tjek("barn-filteret bruger deltagere og målgruppe", () => {
  assert.deepEqual(filtrér([tråd, opslag], { barn: "111" }), [tråd]);
  assert.equal(filtrér([tråd, opslag], { barn: "222" }).length, 0);
});

tjek("et opslag uden målgruppe gælder alle børn", () => {
  // Aula sender ikke altid en målgruppe, og så må et opslag ikke forsvinde
  // bare fordi barnet ikke stod i listen.
  const uden = { post_id: "p2", title: "Skolefest", is_unread: false, starred: false };
  assert.equal(filtrér([uden], { barn: "222" }).length, 1);
});

tjek("kalenderposter filtreres på profil-id", () => {
  const emil = { event_id: "e1", title: "Matematik", profile_ids: ["111"], starred: false };
  const mia = { event_id: "e2", title: "Idræt", profile_ids: ["222"], starred: false };
  assert.deepEqual(filtrér([emil, mia], { barn: "111" }), [emil]);
  assert.deepEqual(filtrér([emil, mia], { barn: "222" }), [mia]);
  assert.equal(filtrér([emil, mia], { barn: "alle" }).length, 2);
});

tjek("flere filtre lægges oven i hinanden", () => {
  assert.equal(filtrér([tråd, opslag], { søg: "lærer", tilstand: "ulast" }).length, 1);
  assert.equal(filtrér([tråd, opslag], { søg: "lærer", tilstand: "stjernet" }).length, 1);
  assert.equal(filtrér([tråd, opslag], { søg: "hej", tilstand: "stjernet" }).length, 0);
});

tjek("filtrene tåler rækker uden felter", () => {
  assert.equal(filtrér([{}], { søg: "noget", tilstand: "ulast" }).length, 0);
  assert.equal(filtrér([{}], { barn: "111" }).length, 1);
});

tjek("status viser ulæste og sidste synk", () => {
  sætFilters();
  sætIState("state.aula", {
    configured: true, unread_threads: 2, unread_posts: 1,
    last_sync: "2026-10-05T08:00:00+00:00", last_error: "",
  });
  assert.equal(aulaStatusText(), "3 ulæste · Opdateret 5. okt. 10.00");
});

tjek("uden login står der, at Aula ikke er logt ind", () => {
  sætFilters();
  sætIState("state.aula", { configured: false, unread_threads: 0, unread_posts: 0, last_sync: "", last_error: "" });
  assert.equal(aulaStatusText(), "Ikke logt ind på Aula");
});

tjek("sidste fejl får prioritet over tidspunktet", () => {
  sætFilters();
  sætIState("state.aula", {
    configured: true, unread_threads: 0, unread_posts: 0,
    last_sync: "2026-10-05T08:00:00+00:00", last_error: "Aula svarede ikke",
  });
  assert.equal(aulaStatusText(), "Sidste fejl: Aula svarede ikke");
});

tjek("skoleabonnementer kan lånes som kalenderfallback", () => {
  const laant = aulaFallbackEvent({
    id: 42, title: "Matematik", location: "1. sal", source_name: "Emils skole",
    start_at: "2026-10-06T08:00:00+00:00", end_at: "2026-10-06T08:45:00+00:00",
  });
  assert.equal(laant.event_id, "school-42");
  assert.equal(laant.title, "Matematik");
  assert.equal(laant.category, "Emils skole");
  // Fallback-ordrer skal aldrig kunne stjernesættes, de hører til Skoleskema.
  assert.equal(laant.starred, false);
  assert.equal(laant.from_school_fallback, true);
  assert.equal(filtrér([laant], { barn: "111" }).length, 1);
});

tjek("emner med html escapes, før de vises", () => {
  const farlig = { post_id: "p3", title: "<script>alert(1)</script>", is_unread: true, starred: false };
  const html = context.aulaPostItem(farlig);
  assert.ok(!html.includes("<script>"));
  assert.ok(html.includes("&lt;script&gt;"));
});

tjek("ulæste emner får en ny-badge, læste ikke", () => {
  assert.ok(context.aulaPostItem({ ...opslag, is_unread: true }).includes('class="panel aula-item unread"'));
  assert.ok(context.aulaPostItem({ ...opslag, is_unread: true }).includes("aula-badge\">Ny<"));
  assert.ok(!context.aulaPostItem(opslag).includes('class="panel aula-item unread"'));
  assert.ok(!context.aulaPostItem(opslag).includes("aula-badge\">Ny<"));
});

tjek("stjerneknappen følger den lokale stjerne", () => {
  const med = context.aulaPostItem(opslag);
  assert.ok(med.includes('data-aula-star="post"'));
  assert.ok(med.includes('aria-pressed="true"'));
  assert.ok(med.includes("★"));
  const uden = context.aulaPostItem({ ...opslag, starred: false });
  assert.ok(uden.includes('aria-pressed="false"'));
});

tjek("opslag uden indhold giver ingen tom brødtekst", () => {
  const tomt = context.aulaPostItem({ post_id: "p4", title: "Kun en titel", is_unread: false, starred: false });
  assert.ok(!tomt.includes('class="aula-body"'));
});

tjek("beskedtråd viser deltagere og kan åbnes", () => {
  const html = context.aulaThreadItem(tråd);
  assert.ok(html.includes('data-aula-open="t1"'));
  assert.ok(html.includes("Lærer · Emil"));
  assert.ok(html.includes('class="panel aula-item unread"'));
});

tjek("åbnet tråd indlæser beskederne", () => {
  sætIState("state.aulaOpenThread", "t1");
  const indlæser = context.aulaThreadItem(tråd);
  assert.ok(indlæser.includes("Indlæser beskeder"));
});

tjek("hentede beskeder vises med afsender og tid", () => {
  sætIState("state.aulaOpenThread", "t1");
  sætIState("state.aulaMessages", { t1: [
    { message_id: "m1", sender: "Lærer", body: "Hej med dig", sent_at: "2026-10-05T08:00:00+00:00", is_from_me: false },
  ] });
  const html = context.aulaThreadItem(tråd);
  assert.ok(html.includes("Hej med dig"));
  assert.ok(html.includes("Lærer · 5. okt. 10.00"));
  sætIState("state.aulaMessages", {});
  sætIState("state.aulaOpenThread", "");
});

tjek("egen besked markeres, så skærmen kan vise hvad der er svaret", () => {
  sætIState("state.aulaOpenThread", "t1");
  sætIState("state.aulaMessages", { t1: [
    { message_id: "m2", sender: "Mig", body: "Tak", sent_at: "2026-10-05T09:00:00+00:00", is_from_me: true },
  ] });
  assert.ok(context.aulaThreadItem(tråd).includes('class="aula-message own"'));
  sætIState("state.aulaMessages", {});
  sætIState("state.aulaOpenThread", "");
});

tjek("kalenderpost viser sted og barn", () => {
  const html = context.aulaEventItem(
    { event_id: "e1", title: "Matematik", location: "1. sal", start_at: "2026-10-06T08:00:00+00:00", category: "Fag", starred: true },
    børn[0],
  );
  assert.ok(html.includes("1. sal · Fag · Emil"));
  assert.ok(html.includes('data-aula-star="event"'));
});

tjek("fallback-ordrer kan ikke stjernesættes fra Aula-siden", () => {
  const html = context.aulaEventItem(
    { event_id: "school-42", title: "Matematik", start_at: "2026-10-06T08:00:00+00:00", starred: false, from_school_fallback: true },
    null,
  );
  assert.ok(!html.includes("data-aula-star"));
  assert.ok(html.includes("fra skoleabonnement"));
});

tjek("barnlisten dannes, men skjules når Aula ikke kender børn", () => {
  sætFilters();
  sætIState("state.aula", { children: børn, configured: true });
  context.renderAulaChildFilter();
  const markup = felter["#aula-child-filter"].innerHTML;
  assert.ok(markup.includes('value="111"'));
  assert.ok(markup.includes("Emil"));
  assert.ok(markup.includes("Mia"));
  assert.equal(felter["#aula-child-filter"].hidden, false);

  sætIState("state.aula", { children: [], configured: true });
  context.renderAulaChildFilter();
  assert.equal(felter["#aula-child-filter"].hidden, true);
});

tjek("et filter der ikke længere findes falder tilbage til alle børn", () => {
  sætFilters({ barn: "999" });
  sætIState("state.aula", { children: børn, configured: true });
  context.renderAulaChildFilter();
  assert.equal(felter["#aula-child-filter"].value, "alle");
});

tjek("opslagsliste får antal i værktøjslinjen", () => {
  sætFilters();
  sætIState("state.aula", {
    configured: true, children: børn, threads: [tråd, opslag],
    posts: [opslag], events: [], unread_threads: 1, unread_posts: 0, last_sync: "", last_error: "",
  });
  sætIState("state.aulaTab", "messages");
  context.renderAula();
  assert.equal(felter["#aula-count"].textContent, "2 beskedtråde");
  sætIState("state.aulaTab", "posts");
  context.renderAula();
  assert.equal(felter["#aula-count"].textContent, "1 opslag");
  sætIState("state.aulaTab", "calendar");
  context.renderAula();
  assert.equal(felter["#aula-count"].textContent, "0 kalenderposter");
  assert.ok(felter["#aula-calendar-list"].innerHTML.includes("kalenderposter, der matcher filteret"));
});

tjek("uden login skjuler vi opslagsfanen bag et klart spørgsmål", () => {
  sætFilters();
  sætIState("state.aula", { configured: false, children: [], threads: [], posts: [], events: [], unread_threads: 0, unread_posts: 0, last_sync: "", last_error: "" });
  sætIState("state.aulaTab", "posts");
  context.renderAulaSetup();
  assert.equal(felter["#aula-setup"].hidden, false);
  assert.ok(felter["#aula-code-fields"].hidden, "kodefelterne skal være skjult, indtil et login er startet");
});

tjek("efter login skjuler vi opslagskortet helt", () => {
  sætFilters();
  sætIState("state.aula", { configured: true, children: børn, threads: [], posts: [], events: [], unread_threads: 0, unread_posts: 0, last_sync: "", last_error: "" });
  context.renderAulaSetup();
  assert.equal(felter["#aula-setup"].hidden, true);
});

tjek("et igangværende login viser kodefelterne", () => {
  sætFilters();
  sætIState("state.settings", { aula_login_pending: true });
  sætIState("state.aula", { configured: false, children: [], threads: [], posts: [], events: [], unread_threads: 0, unread_posts: 0, last_sync: "", last_error: "" });
  context.renderAulaSetup();
  assert.equal(felter["#aula-code-fields"].hidden, false);
  assert.ok(felter["#aula-login-hint"].textContent.includes("koden"));
});

if (fejlene.length) {
  process.stdout.write(`\n${bestået} af ${bestået + fejlene.length} tjek bestaaet\n\nFejl i første fejlede tjek:\n`);
  throw fejlene[0];
}
process.stdout.write(`\n${bestået} tjek gennem Aula-sidens logik, alle bestaaet.\n`);
