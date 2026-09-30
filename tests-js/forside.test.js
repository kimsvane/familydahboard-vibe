// Kører den rigtige app.js i en minimal DOM-strump, så de rene funktioner
// på forsiden kan testes uden en browser.
const fs = require("fs");
const path = require("path");
const vm = require("vm");
const assert = require("assert");

const source = fs.readFileSync(path.join(__dirname, "..", "web", "app.js"), "utf8");

// app.js kalder init() nederst i sig selv, som forsøger at tale med en
// server og registrere en service worker. navigator skal derfor se ud
// som om den hverken har fetch eller serviceWorker, ellers støjer testen.
let initKaldt = 0;
const kontekstTimere = [];
// Struppens cache-stand. Den ligger fast i konteksten, fordi en cache
// der swappes under en test er svær at holde styr på.
const cacheNøgler = new Set();
const cacheSletninger = [];
let cacheFejl = false;
let reloadTalt = 0;

const noopElement = {
  hidden: false,
  textContent: "",
  innerHTML: "",
  value: "",
  checked: false,
  classList: { add() {}, remove() {}, toggle() {} },
  setAttribute() {},
  getAttribute() { return null; },
  removeAttribute() {},
  addEventListener() {},
  append() {},
  remove() {},
  querySelector() { return noopElement; },
  querySelectorAll() { return []; },
  appendChild() {},
  getBoundingClientRect() { return { height: 0, width: 0 }; },
  focus() {},
  dataset: {},
  style: {},
  className: "",
};

const context = {
  document: {
    documentElement: { style: { setProperty() {} } },
    body: { classList: { toggle() {} }, dataset: {} },
    cookie: "",
    querySelector: () => noopElement,
    querySelectorAll: () => [],
    getElementById: () => noopElement,
    createElement: () => ({ ...noopElement }),
    addEventListener() {},
  },
  localStorage: { getItem: () => null, setItem() {} },
  navigator: {},
  window: {
    addEventListener() {},
    scrollTo() {},
    setTimeout: (fn, ms) => { kontekstTimere.push({ fn, ms }); return kontekstTimere.length; },
    caches: {
      keys: async () => {
        if (cacheFejl) throw new Error("ingen adgang");
        return [...cacheNøgler];
      },
      delete: async (nøgle) => { cacheSletninger.push(nøgle); return cacheNøgler.delete(nøgle); },
    },
  },
  location: { origin: "http://test", reload() { reloadTalt += 1; } },
  setTimeout: (fn, ms) => { kontekstTimere.push({ fn, ms }); return kontekstTimere.length; },
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

const { familyEventsInNext24h, weatherGlyph, toDisplayTemperature, themeForNow, clockMinutes,
  renderForsideHints, renderReminderSourceOptions, reminderSourceLabel, reminderMatchLabel,
  genindlaesAlt } = context;

let bestævrelser = 0;
// Enkelte tjek er asynkrone, så de skal vente. De køres i rækkefølge og
// én ad gangen, ellers ville to asynkrone tjek oveni hinanden og skrive
// over hinandens stramper. Et fejlet tjek stopper ikke de andre, men
// bliver gemt og kastet til sidst.
let kæde = Promise.resolve();
const fejlene = [];
const tjek = (navn, kør) => {
  kæde = kæde.then(() => kør()).then(
    () => {
      bestævrelser += 1;
      process.stdout.write(`  ok  ${navn}\n`);
    },
    (fejl) => {
      fejlene.push(fejl);
      process.stdout.write(`  FEJL  ${navn}\n`);
    },
  );
};
const afslut = (tekst) => kæde.then(() => {
  if (fejlene.length) {
    process.stdout.write(`\n${bestævrelser} af ${bestævrelser + fejlene.length} tjek bestaaet\n\nFejl i første fejlede tjek:\n`);
    throw fejlene[0];
  }
  process.stdout.write(`\n${bestævrelser} ${tekst}\n`);
});

tjek("familieEventsInNext24faktiskFindes", () => {
  assert.equal(typeof familyEventsInNext24h, "function");
});

tjek("en aftale der starter om to timer kommer med", () => {
  const nu = new Date("2026-10-03T12:00:00");
  const events = [{ id: "1", title: "Møde", start_at: "2026-10-03T14:00:00+02:00", end_at: "2026-10-03T15:00:00+02:00", source_kind: "calendar" }];
  assert.equal(familyEventsInNext24h(events, nu).length, 1);
});

tjek("en aftale der er slut forsvinder", () => {
  const nu = new Date("2026-10-03T12:00:00");
  const events = [{ id: "1", title: "Møde", start_at: "2026-10-03T08:00:00+02:00", end_at: "2026-10-03T09:00:00+02:00", source_kind: "calendar" }];
  assert.equal(familyEventsInNext24h(events, nu).length, 0);
});

tjek("en aftale der er i gang tæller stadig", () => {
  // Ellers forsvandt den praecis mens folkene gik hjem.
  const nu = new Date("2026-10-03T12:00:00");
  const events = [{ id: "1", title: "Møde", start_at: "2026-10-03T11:00:00+02:00", end_at: "2026-10-03T13:00:00+02:00", source_kind: "calendar" }];
  assert.equal(familyEventsInNext24h(events, nu).length, 1);
});

tjek("skoleaftaler holdes ude, de har deres egen side", () => {
  const nu = new Date("2026-10-03T12:00:00");
  const events = [{ id: "1", title: "Idræt", start_at: "2026-10-03T14:00:00+02:00", end_at: "2026-10-03T15:00:00+02:00", source_kind: "school" }];
  assert.equal(familyEventsInNext24h(events, nu).length, 0);
});

tjek("en aftale der er mere end 24 timer væk holdes ude", () => {
  const nu = new Date("2026-10-03T12:00:00");
  const events = [{ id: "1", title: "Senere", start_at: "2026-10-05T14:00:00+02:00", end_at: "2026-10-05T15:00:00+02:00", source_kind: "calendar" }];
  assert.equal(familyEventsInNext24h(events, nu).length, 0);
});

tjek("heldagsbegivenheder i dag og i morgen kommer med", () => {
  const nu = new Date("2026-10-03T20:00:00");
  const events = [
    { id: "1", title: "I dag", all_day: true, local_date: "2026-10-03", source_kind: "calendar" },
    { id: "2", title: "I morgen", all_day: true, local_date: "2026-10-04", source_kind: "calendar" },
    { id: "3", title: "Om tre dage", all_day: true, local_date: "2026-10-06", source_kind: "calendar" },
  ];
  const ids = familyEventsInNext24h(events, nu).map((e) => e.id);
  assert.deepEqual(ids, ["1", "2"]);
});

tjek("aftaler sorteres efter tid", () => {
  const nu = new Date("2026-10-03T12:00:00");
  const events = [
    { id: "sen", title: "Sen", start_at: "2026-10-03T20:00:00+02:00", end_at: "2026-10-03T21:00:00+02:00", source_kind: "calendar" },
    { id: "før", title: "Før", start_at: "2026-10-03T13:00:00+02:00", end_at: "2026-10-03T14:00:00+02:00", source_kind: "calendar" },
  ];
  assert.deepEqual(familyEventsInNext24h(events, nu).map((e) => e.id), ["før", "sen"]);
});

tjek("ugyldig tid springes over frem for at stoppe alt", () => {
  const nu = new Date("2026-10-03T12:00:00");
  const events = [
    { id: "1", title: "Ødelagt", start_at: "ikke-en-dato", source_kind: "calendar" },
    { id: "2", title: "God", start_at: "2026-10-03T14:00:00+02:00", end_at: "2026-10-03T15:00:00+02:00", source_kind: "calendar" },
  ];
  assert.deepEqual(familyEventsInNext24h(events, nu).map((e) => e.id), ["2"]);
});

tjek("alle WMO-vejrkoder har et tegn", () => {
  // Ellers falder almindelig regn tilbage til klipletegnet paa forsiden.
  const koder = [0, 1, 2, 3, 45, 48, 51, 53, 55, 56, 57, 61, 63, 65, 66, 67, 71, 73, 75, 77, 80, 81, 82, 85, 86, 95, 96, 99];
  for (const kode of koder) {
    const tegn = weatherGlyph(kode);
    assert.notEqual(tegn, "◌", `kode ${kode} mangler et tegn`);
  }
});

tjek("temperatur omregnes til fahrenheit naar det er valgt", () => {
  assert.equal(toDisplayTemperature(20, { temperature_unit: "celsius" }), 20);
  assert.equal(Math.round(toDisplayTemperature(20, { temperature_unit: "fahrenheit" })), 68);
  assert.equal(toDisplayTemperature(null, {}), null);
  assert.equal(toDisplayTemperature("ingen værdi", {}), null);
});

// Datoer laves med tal, ikke strenge, så testen er uafhængig af hvilken
// tidszone den kører i. 2026-10-03 er en lørdag, men det er ligegyldigt
// her: kun klokkeslættet er tale om.
const klokken = (time) => {
  const [time_, minut] = time.split(":").map(Number);
  return new Date(2026, 9, 3, time_, minut, 0);
};

tjek("temaForNow findes", () => {
  assert.equal(typeof themeForNow, "function");
});

tjek("manuelt valgt tema ignorerer uret", () => {
  // En mørk vægskærm skal ikke skifte, bare fordi det er blevet morgen.
  assert.equal(themeForNow({ theme: "dark" }, klokken("12:00")), "dark");
  assert.equal(themeForNow({ theme: "light" }, klokken("23:00")), "light");
  assert.equal(themeForNow({ theme: "LIGHT" }, klokken("23:00")), "light");
});

tjek("automatisk tema er lyst om dagen og mørkt om aftenen", () => {
  const indstillinger = { theme: "auto", theme_day_start: "07:00", theme_night_start: "20:00" };
  assert.equal(themeForNow(indstillinger, klokken("06:59")), "dark");
  assert.equal(themeForNow(indstillinger, klokken("07:00")), "light");
  assert.equal(themeForNow(indstillinger, klokken("12:00")), "light");
  assert.equal(themeForNow(indstillinger, klokken("19:59")), "light");
  assert.equal(themeForNow(indstillinger, klokken("20:00")), "dark");
  assert.equal(themeForNow(indstillinger, klokken("23:30")), "dark");
  assert.equal(themeForNow(indstillinger, klokken("03:00")), "dark");
});

tjek("egne klokkeslaet bliver brugt", () => {
  const indstillinger = { theme: "auto", theme_day_start: "09:30", theme_night_start: "22:15" };
  assert.equal(themeForNow(indstillinger, klokken("09:00")), "dark");
  assert.equal(themeForNow(indstillinger, klokken("09:30")), "light");
  assert.equal(themeForNow(indstillinger, klokken("22:14")), "light");
  assert.equal(themeForNow(indstillinger, klokken("22:15")), "dark");
});

tjek("nat der gaar over midnat stadig virker", () => {
  // Dag efter nat, som naar man vil have lys sent og tidligt. Intervallet
  // er saaledes 22:00-06:00, og det er den anden rækkefoelge end normal.
  const indstillinger = { theme: "auto", theme_day_start: "22:00", theme_night_start: "06:00" };
  assert.equal(themeForNow(indstillinger, klokken("23:00")), "light");
  assert.equal(themeForNow(indstillinger, klokken("05:59")), "light");
  assert.equal(themeForNow(indstillinger, klokken("06:00")), "dark");
  assert.equal(themeForNow(indstillinger, klokken("12:00")), "dark");
  assert.equal(themeForNow(indstillinger, klokken("21:59")), "dark");
  assert.equal(themeForNow(indstillinger, klokken("22:00")), "light");
});

tjek("manglende eller ugyldige tidsfelter falder tilbage paa fornuft", () => {
  // Ellers ville en tom indstilling gøre hele vaegskaermen hvid.
  assert.equal(themeForNow({}, klokken("12:00")), "light");
  assert.equal(themeForNow({}, klokken("23:00")), "dark");
  assert.equal(themeForNow({ theme: "auto", theme_day_start: "halv syv" }, klokken("23:00")), "dark");
  assert.equal(themeForNow(null, klokken("12:00")), "light");
});

tjek("clockMinutes regner om og afviser vrøvl", () => {
  assert.equal(clockMinutes("07:30", 0), 450);
  assert.equal(clockMinutes("00:00", 5), 0);
  assert.equal(clockMinutes("23:59", 5), 1439);
  assert.equal(clockMinutes("25:00", 5), 5);
  assert.equal(clockMinutes("07:99", 5), 5);
  assert.equal(clockMinutes("0700", 5), 5);
  assert.equal(clockMinutes("", 5), 5);
  assert.equal(clockMinutes(null, 5), 5);
  assert.equal(clockMinutes(undefined, 5), 5);
});

/* ================= Husk paa aftaler ================= */

// Grib det element renderForsideHints skriver i, saa vi kan se
// resultatet uden en browser.
function medHuskeTarget(kør) {
  const fanget = { hidden: false, innerHTML: "" };
  const gemt = context.document.querySelector;
  context.document.querySelector = (vælger) => (vælger === "#forside-hints" ? fanget : noopElement);
  try { kør(fanget); } finally { context.document.querySelector = gemt; }
  return fanget;
}

tjek("renderForsideHints findes", () => {
  assert.equal(typeof renderForsideHints, "function");
});

tjek("uden aktive huskelinjer skjuler vi hele boksen", () => {
  // En tom boks med overskriften "Husk" paa vaeggen er bare stoer.
  const mål = medHuskeTarget((el) => renderForsideHints([], el));
  assert.equal(mål.hidden, true);
  assert.equal(mål.innerHTML, "");
  const uden = medHuskeTarget((el) => renderForsideHints(undefined, el));
  assert.equal(uden.hidden, true);
});

tjek("en aktiv huskelinje viser teksten", () => {
  const mål = medHuskeTarget((el) => renderForsideHints([{ text: "Husk gymnastiktøj", local_time: "10:00", in_progress: false }], el));
  assert.equal(mål.hidden, false);
  assert.ok(mål.innerHTML.includes("Husk gymnastiktøj"));
  assert.ok(mål.innerHTML.includes("forside-hint"));
  assert.ok(mål.innerHTML.includes("10:00"));
});

tjek("titel med HTML-escapes", () => {
  // Ellers kunne en kalendertitel faa script ind paa vaeggen.
  const mål = medHuskeTarget((el) => renderForsideHints([{ text: "<script>alert(1)</script>", local_time: "10:00" }], el));
  assert.ok(!mål.innerHTML.includes("<script>"));
  assert.ok(mål.innerHTML.includes("&lt;script&gt;"));
});

tjek("igang-i-gang og heldagsaftaler faar en forstaaelig tid", () => {
  const igang = medHuskeTarget((el) => renderForsideHints([{ text: "Husk sko", in_progress: true, local_time: "10:00" }], el));
  assert.ok(igang.innerHTML.includes("igang"));
  const hel = medHuskeTarget((el) => renderForsideHints([{ text: "Husk sko", all_day: true, local_time: "00:00" }], el));
  assert.ok(hel.innerHTML.includes("Hele dagen"));
});

tjek("flere huskelinjer vises alle", () => {
  const mål = medHuskeTarget((el) => renderForsideHints([
    { text: "Husk gymnastiktøj", local_time: "10:00" },
    { text: "Husk sko", local_time: "14:00" },
    { text: "Husk idrætstøj", local_time: "08:00" },
  ], el));
  const antal = (mål.innerHTML.match(/forside-hint__tekst/g) || []).length;
  assert.equal(antal, 3);
});

tjek("huskelinjer har hverken klokketid eller tid ved heldag", () => {
  // Uden tid skal der ikke ligge en tom span.
  const mål = medHuskeTarget((el) => renderForsideHints([{ text: "Husk noget", local_time: "" }], el));
  assert.ok(!mål.innerHTML.includes("forside-hint__hvornår"));
});

tjek("kildelisten følger den valgte kalendertype", () => {
  // Kalendertypen drives af value, listen skrives til innerHTML.
  const type = { value: "school" };
  const liste = { innerHTML: "", value: "" };
  const gemt = context.document.querySelector;
  context.document.querySelector = (vælger) => {
    if (vælger === "#reminder-source-kind") return type;
    if (vælger === "#reminder-source-id") return liste;
    return noopElement;
  };
  const kilder = [
    { id: 1, name: "Familien", kind: "calendar" },
    { id: 2, name: "Skolen", kind: "school" },
  ];
  try {
    renderReminderSourceOptions(kilder);
    // Skoleskema som valgt type: kun skolen i listen.
    assert.ok(liste.innerHTML.includes("Skolen"));
    assert.ok(!liste.innerHTML.includes("Familien"));
    type.value = "calendar";
    renderReminderSourceOptions(kilder);
    assert.ok(liste.innerHTML.includes("Familien"));
    assert.ok(!liste.innerHTML.includes("Skolen"));
  } finally { context.document.querySelector = gemt; }
});

/* ================= Genindlaes alt ================= */

tjek("genindlaesAlt findes", () => {
  assert.equal(typeof genindlaesAlt, "function");
});

tjek("genindlaesAlt rydder cachen og genindlaeser siden", async () => {
  // Vi vil ikke have en gammel app.js liggende i service workerens
  // cache, hvis serveren er us tilgaengelig i oieblikket.
  cacheNøgler.clear();
  cacheSletninger.length = 0;
  cacheFejl = false;
  cacheNøgler.add("gammel-cache");
  cacheNøgler.add("family-dashboard-abc");
  reloadTalt = 0;
  const gemte = kontekstTimere.splice(0, kontekstTimere.length);
  try {
    await genindlaesAlt();
    assert.deepEqual(cacheSletninger.slice().sort(), ["family-dashboard-abc", "gammel-cache"]);
    // Reloaden skal ligge i en timer, saa beskeden kan tegnes foerst.
    assert.equal(reloadTalt, 0);
    const planlagt = kontekstTimere.splice(0, kontekstTimere.length);
    // showToast rydder sin egen kliche, genindlaesAlt kalder reload.
    planlagt.forEach((t) => t.fn());
    assert.equal(reloadTalt, 1);
  } finally {
    kontekstTimere.push(...gemte);
  }
});

tjek("genindlaesAlt virker ogsa naar cachen er lukket", async () => {
  // En browser uden cache-tilgang skal ikke gaa i stykker, fordi
  // knappen er den eneste vej til en frisk side.
  cacheFejl = true;
  reloadTalt = 0;
  const gemte = kontekstTimere.splice(0, kontekstTimere.length);
  try {
    await genindlaesAlt();
    kontekstTimere.splice(0, kontekstTimere.length).forEach((t) => t.fn());
    assert.equal(reloadTalt, 1);
  } finally {
    cacheFejl = false;
    kontekstTimere.push(...gemte);
  }
});

afslut("tjek gennemforsidens logik, alle bestaaet.");
