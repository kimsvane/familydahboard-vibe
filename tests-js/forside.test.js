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
  window: { addEventListener() {}, scrollTo() {}, location: { origin: "http://test", reload() {} } },
  location: { origin: "http://test", reload() {} },
  setTimeout() {},
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

const { familyEventsInNext24h, weatherGlyph, toDisplayTemperature } = context;

let bestævrelser = 0;
const tjek = (navn, kør) => {
  kør();
  bestævrelser += 1;
  process.stdout.write(`  ok  ${navn}\n`);
};

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

console.log(`\n${bestævrelser} tjek gennemforsidens logik, alle bestaaet.`);
