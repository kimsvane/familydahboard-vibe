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
// Timere får rigtige id'er, så clearTimeout kan fjerne præcis den
// timer, der blev sat, i stedet for at være en tom funktion. Det er
// nødvendigt for at teste, at en popup-lukning IKKE nulstilles af et
// senere poll.
let næsteTimerId = 1;
function planlægTimer(fn, ms) {
  const id = næsteTimerId++;
  kontekstTimere.push({ id, fn, ms });
  return id;
}
function rydTimer(id) {
  const index = kontekstTimere.findIndex((t) => t.id === id);
  if (index >= 0) kontekstTimere.splice(index, 1);
}
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
    setTimeout: planlægTimer,
    clearTimeout: rydTimer,
    setInterval: () => 1,
    clearInterval() {},
    caches: {
      keys: async () => {
        if (cacheFejl) throw new Error("ingen adgang");
        return [...cacheNøgler];
      },
      delete: async (nøgle) => { cacheSletninger.push(nøgle); return cacheNøgler.delete(nøgle); },
    },
  },
  location: { origin: "http://test", reload() { reloadTalt += 1; } },
  setTimeout: planlægTimer,
  setInterval() { return { unref() {} }; },
  clearTimeout: rydTimer,
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
  genindlaesAlt, handleDetections, pollCameras, renderForsideBirthday } = context;

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

// Opsætter et kamera i state og en observerbar popup, så popup-logikken
// kan testes. Gemmer struppene bagefter, så resten af testene er urørt.
function medEntréKamera(kør) {
  const gemtQuery = context.document.querySelectorAll;
  const popup = context.document.querySelector("#detect-popup");
  const liveBadge = { synlig: false, removeAttribute(navn) { if (navn === "hidden") this.synlig = true; } };
  const img = { dataset: { detectCam: "1" }, src: "", closest: () => ({ querySelector: () => liveBadge }) };
  context.document.querySelectorAll = (vælger) => (vælger.includes("data-detect-cam") ? [img] : []);
  popup.innerHTML = "";
  popup.hidden = true;
  // Hvert tjek skal starte forfra. detectKey overlever ellers fra
  // forrige tjek, og så går poppen aldrig ind i den nye hændelse.
  vm.runInContext("state.detectKey = null; stopDetectTimers();", context);
  vm.runInContext('state.cameras = [{ id: 1, name: "Entré", live_stream_url: "rtsp://bruger:kod@10.0.0.5:554/h264Preview_01_main" }]', context);
  try {
    return kør({ popup, img, liveBadge });
  } finally {
    context.document.querySelectorAll = gemtQuery;
  }
}

const aktivitet = (ekstra = {}) => ({
  active: [{ id: 1, name: "Entré", types: [{ type: "people", label: "Person" }], since: "2026-09-30T08:00:00+00:00" }],
  recent: [],
  close_delay: 0,
  live_delay: 3,
  poll_seconds: 5,
  ...ekstra,
});

tjek("popup viser stillbillede med det samme og tænder live-feed først efter forsinkelsen", () => {
  medEntréKamera(({ popup, img, liveBadge }) => {
    kontekstTimere.splice(0, kontekstTimere.length);
    handleDetections(aktivitet());
    assert.equal(popup.hidden, false, "popup skal være synlig med det samme");
    assert.ok(popup.innerHTML.includes("/api/cameras/1/snapshot"), "skal starte med et stillbillede");
    assert.ok(!popup.innerHTML.includes("/stream?"), "må ikke starte den tunge stream med det samme");
    assert.equal(liveBadge.synlig, false, "LIVE-badgen må ikke lyse endnu");
    const planlagt = kontekstTimere.splice(0, kontekstTimere.length);
    assert.equal(planlagt.length, 1, "der skal være præcis én forsinkelse");
    assert.equal(planlagt[0].ms, 3000, "forsinkelsen skal følge live_delay");
    planlagt[0].fn();
    assert.ok(img.src.includes("/api/cameras/1/stream?max_seconds=300"), "skal skifte til live-stream");
    assert.equal(liveBadge.synlig, true, "LIVE-badgen skal tænde når streamen kører");
  });
});

tjek("live_delay 0 tænder streamen uden at vente", () => {
  medEntréKamera(({ img }) => {
    kontekstTimere.splice(0, kontekstTimere.length);
    handleDetections(aktivitet({ live_delay: 0 }));
    assert.ok(img.src.includes("/stream?"), "skal starte med det samme når forsinkelsen er 0");
  });
});

tjek("popup bygges ikke om ved hvert poll, så live-streamen overlever", () => {
  medEntréKamera(({ popup }) => {
    kontekstTimere.splice(0, kontekstTimere.length);
    handleDetections(aktivitet());
    const første = popup.innerHTML;
    // Samme hændelse polled igen. Før blev innerHTML sat på ny hver
    // gang, hvilket ødelagde <img>-elementet og dræbte live-streamen.
    handleDetections(aktivitet());
    handleDetections(aktivitet());
    assert.equal(popup.innerHTML, første, "poppen må ikke bygges om under den samme hændelse");
  });
});

tjek("popup lukker sig selv, og et poll undervejs ødelægger ikke nedlukningen", () => {
  medEntréKamera(({ popup, img }) => {
    kontekstTimere.splice(0, kontekstTimere.length);
    handleDetections(aktivitet({ close_delay: 12 }));
    const lukning = kontekstTimere.find((t) => t.ms === 12000);
    assert.ok(lukning, "nedlukning skal planlægges én gang");
    // Et poll imellem må ikke fjerne nedlukningen. Før blev timeren
    // nulstillet ved hvert poll, så poppen aldrig lukkede sig selv.
    handleDetections(aktivitet({ close_delay: 12 }));
    assert.ok(kontekstTimere.some((t) => t.id === lukning.id), "nedlukningen skal stadig stå");
    assert.equal(popup.hidden, false, "popup skal stadig være synlig inden nedlukningen");
    lukning.fn();
    assert.equal(popup.hidden, true, "popup skal lukke sig selv");
    assert.equal(popup.innerHTML, "", "billederne skal ryddes, så ffmpeg stopper");
    assert.equal(kontekstTimere.some((t) => t.ms === 12000), false, "der må ikke stå en lukning tilbage");
  });
});

tjek("ny hændelse efter lukning åbner poppen igen", () => {
  medEntréKamera(({ popup, img }) => {
    kontekstTimere.splice(0, kontekstTimere.length);
    handleDetections(aktivitet({ close_delay: 12 }));
    kontekstTimere.find((t) => t.ms === 12000).fn();
    assert.equal(popup.hidden, true);
    // Ny hændelse = ny since. Skal starte forfra med et stillbillede.
    handleDetections(aktivitet({ live_delay: 3, close_delay: 12, active: [
      { id: 1, name: "Entré", types: [{ type: "people", label: "Person" }], since: "2026-09-30T09:00:00+00:00" },
    ] }));
    assert.equal(popup.hidden, false, "den nye hændelse skal åbne poppen igen");
    assert.ok(popup.innerHTML.includes("/snapshot?"), "den nye hændelse skal starte med stillbillede");
    assert.equal(img.src, "", "det nye billede må ikke arve den gamle stream");
  });
});

tjek("poppen forsvinder når aktiviteten stopper", () => {
  medEntréKamera(({ popup }) => {
    kontekstTimere.splice(0, kontekstTimere.length);
    handleDetections(aktivitet());
    handleDetections({ ...aktivitet(), active: [] });
    assert.equal(popup.hidden, true, "ingen aktivitet skal lukke poppen");
    assert.equal(popup.innerHTML, "", "billederne skal ryddes, så ffmpeg stopper");
    assert.equal(kontekstTimere.length, 0, "der må ikke stå timere tilbage");
  });
});

tjek("et kamera uden RTSP-url får intet live-feed", () => {
  const gemtQuery = context.document.querySelectorAll;
  const popup = context.document.querySelector("#detect-popup");
  context.document.querySelectorAll = () => [];
  popup.innerHTML = "";
  vm.runInContext('state.cameras = [{ id: 1, name: "Entré", live_stream_url: "" }]', context);
  try {
    kontekstTimere.splice(0, kontekstTimere.length);
    handleDetections(aktivitet());
    assert.ok(popup.innerHTML.includes("/snapshot"), "skal bruge stillbilledet");
    assert.ok(!popup.innerHTML.includes("data-detect-live"), "LIVE-badgen skal ikke vises uden stream");
  } finally {
    context.document.querySelectorAll = gemtQuery;
  }
});

tjek(" ét fejlslag dræber ikke overvågningen, poppen kommer tilbage", async () => {
  // Kiosken står dagevis. Før stoppede ét netværkshul eller en udløbet
  // session altså al overvågning for evigt, og poppen kom aldrig igen.
  const gemtFetch = context.fetch;
  const gemtApi = context.api;
  const kaldte = [];
  let fejlAntal = 0;
  context.api = async (sti) => {
    kaldte.push(sti);
    if (sti === "/api/cameras/activity" && fejlAntal === 0) {
      fejlAntal += 1;
      throw new Error("netværkshul");
    }
    if (sti === "/api/cameras/activity") return aktivitet();
    return { cameras: [], recent: [], active: [] };
  };
  try {
    kontekstTimere.splice(0, kontekstTimere.length);
    await pollCameras();
    assert.ok(
      kontekstTimere.some((t) => t.ms === 5000),
      "der skal være planlagt et nyt forsøg",
    );
    // Forsøget kører, og serveren svarer igen denne gang.
    kontekstTimere.splice(0, kontekstTimere.length).forEach((t) => t.fn());
    await new Promise((løs) => setImmediate(løs));
    assert.ok(kaldte.length >= 2, "den skal prøve igen efter fejlen");
  } finally {
    context.api = gemtApi;
    context.fetch = gemtFetch;
  }
});

tjek("foedselsdagsnavnet faar et dansk flag foran", () => {
  const m = { innerHTML: "" };
  const gemt = context.document.querySelector;
  context.document.querySelector = (vælger) => (vælger === "#forside-birthday" ? m : gemt(vælger));
  try {
    renderForsideBirthday([{ name: "Emma", days_until: 3, age: 40, next_occurrence: "2026-11-02" }]);
    // Flaget skal komme foer navnet, og navnet skal escapes.
    const flag = m.innerHTML.indexOf("fodselsdag__flag");
    const navn = m.innerHTML.indexOf("Emma");
    assert.ok(flag >= 0, "der skal være et flag");
    assert.ok(flag < navn, "flaget skal stå foran navnet");
    assert.ok(m.innerHTML.includes("fodselsdag__navn-tekst"), "navnet skal have sin egen boks, så flaget kan stå i flex");
    // Det er et dekorativt flag, ikke indhold, så skærmlæseren skal springe over.
    assert.ok(m.innerHTML.includes('aria-hidden="true"'), "flaget skal være aria-hidden");
    assert.ok(m.innerHTML.includes('class="fodselsdag__flag"'), "flaget skal have sin egen klasse");
  } finally {
    context.document.querySelector = gemt;
  }
});

tjek("et fodselsdagsnavn med HTML escapes stadig faar flag", () => {
  const m = { innerHTML: "" };
  const gemt = context.document.querySelector;
  context.document.querySelector = (vælger) => (vælger === "#forside-birthday" ? m : gemt(vælger));
  try {
    renderForsideBirthday([{ name: "<script>x</script>", days_until: 1, age: 5, next_occurrence: "2026-01-01" }]);
    assert.ok(m.innerHTML.includes("fodselsdag__flag"), "flaget skal stadig vises");
    assert.ok(!m.innerHTML.includes("<script>"), "navnet skal escapes");
  } finally {
    context.document.querySelector = gemt;
  }
});

afslut("tjek gennemforsidens logik, alle bestaaet.");
