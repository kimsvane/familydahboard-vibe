'use strict';

// main.js starter to skærmblokke, og en ReferenceError i stop-koden ville
// efterlade dem hængende: skærmen ville aldrig slukke igen, og appen ville
// være svær at lukke. node --check finder ikke det, så her indlæser vi
// main.js i en vm med en elektron-stub og følger blokkernes livscyklus.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const { afterEach, beforeEach } = require('node:test');
const vm = require('node:vm');
const Module = require('node:module');

const mainPath = path.join(__dirname, '..', 'src', 'main.js');
const originalLoad = Module._load;

/* main.js' rigtige hjælpemoduler (forbindelse, sensorer) bruger de globale
   timere, og start() fortsætter asynkront efter at den er indlæst. Derfor
   stubbes de for hele testen, ikke kun under indlæsningen, ellers ville
   node --test vente på en genforsøgsløkke, vi ikke tester her. */
const rigtigeTimere = {};
beforeEach(() => {
  for (const navn of ['setTimeout', 'setInterval', 'clearTimeout', 'clearInterval']) {
    rigtigeTimere[navn] = global[navn];
  }
  global.setTimeout = () => 1;
  global.setInterval = () => 1;
  global.clearTimeout = () => {};
  global.clearInterval = () => {};
});
afterEach(() => {
  Object.assign(global, rigtigeTimere);
});

// Electron-opslag fanges her, så main.js kan køre uden rigtig elektron.
function bygElektronStub() {
  const hændelser = { quit: [], 'window-all-closed': [], activate: [], 'second-instance': [] };
  const started = [];
  const stopped = [];
  let næsteId = 1;

  function lavVindue(navn) {
    return {
      navn,
      isDestroyed: () => false,
      isVisible: () => false,
      isMinimized: () => false,
      show() {},
      hide() {},
      showInactive() {},
      focus() {},
      close() {},
      setKiosk() {},
      setAlwaysOnTop() {},
      setFullScreen() {},
      setBounds() {},
      getBounds: () => ({ x: 0, y: 0, width: 1280, height: 800 }),
      loadURL() { return Promise.resolve(); },
      loadFile() { return Promise.resolve(); },
      on() {},
      once() {},
      removeAllListeners() {},
      webContents: { on() {}, send() {}, setWindowOpenHandler() {}, openDevTools() {} },
    };
  }

  const vinduer = [];

  const app = {
    setName() {},
    getPath: () => path.join(__dirname, '..', '.tmp-test'),
    setPath() {},
    requestSingleInstanceLock: () => true,
    quit() { hændelser.quit.push(true); },
    on(name, handler) { hændelser[name] = handler; },
    whenReady: () => Promise.resolve(),
    getVersion: () => '0.0.0-test',
    isPackaged: false,
  };

  const electron = {
    app,
    BrowserWindow: Object.assign(function BrowserWindow() {
      const vindue = lavVindue(`vindue-${vinduer.length}`);
      vinduer.push(vindue);
      return vindue;
    }, { getAllWindows: () => [...vinduer] }),
    ipcMain: { handle() {}, removeHandler() {}, on() {} },
    powerSaveBlocker: {
      start(type) { started.push(type); return næsteId++; },
      stop(id) { stopped.push(id); return true; },
      isStarted() { return true; },
    },
    screen: { on() {}, getPrimaryDisplay: () => ({ workAreaSize: { width: 1280, height: 800 }, bounds: { x: 0, y: 0 } }) },
    session: { defaultSession: { setPermissionRequestHandler() {}, setPermissionCheckHandler() {}, clearStorageData() {} } },
  };
  return { electron, hændelser, started, stopped };
}

function kørMain(stub) {
  Module._load = function (forespørgsel, forælder, erAnden) {
    if (forespørgsel === 'electron') return stub.electron;
    return originalLoad.call(this, forespørgsel, forælder, erAnden);
  };
  // main.js' egne relativ-requires skal løses fra src/, ikke fra testen.
  const mainRequire = Module.createRequire(mainPath);
  const kode = fs.readFileSync(mainPath, 'utf8');
  const kontekst = {
    require: (navn) => mainRequire(navn),
    module: { exports: {} },
    exports: {},
    console,
    __dirname: path.dirname(mainPath),
    __filename: mainPath,
    process,
    Buffer,
    setTimeout: () => 1,
    clearTimeout() {},
    setInterval: () => 1,
    clearInterval() {},
    fetch: async () => { throw new Error('ingen netværk i testen'); },
  };
  kontekst.globalThis = kontekst;
  vm.createContext(kontekst);
  try {
    vm.runInContext(kode, kontekst, { filename: mainPath });
  } finally {
    Module._load = originalLoad;
  }
  return kontekst;
}

// start() går gennem et par await's, så vi lader mikroopgaverne køre færdig.
function udhold() {
  return new Promise((resolve) => process.nextTick(resolve));
}

test('skærmen blokeres for både dvale og display-idle ved start', async () => {
  const stub = bygElektronStub();
  kørMain(stub);
  // start() er asynkron, så vi giver den et øjeblik til at nå powerSaveBlocker.
  await udhold();
  assert.deepEqual(stub.started, ['prevent-app-suspension', 'prevent-display-sleep']);
});

test('begge blokkere stoppes igen ved quit', async () => {
  const stub = bygElektronStub();
  kørMain(stub);
  await udhold();
  const startAntal = stub.started.length;
  assert.ok(startAntal >= 2, 'begge blokkere skal være startet');

  const beforeQuit = stub.hændelser['before-quit'];
  assert.equal(typeof beforeQuit, 'function', 'before-quit skal være tilknyttet');

  // En ReferenceError her ville efterlade blokkerne hængende.
  assert.doesNotThrow(() => beforeQuit());

  // Id'erne fra start() skal stoppes, og hverken mere eller mindre.
  assert.deepEqual(stub.stopped, [1, 2]);
});

test('to quit-kald stopper ikke noget to gange', async () => {
  const stub = bygElektronStub();
  kørMain(stub);
  await udhold();
  const beforeQuit = stub.hændelser['before-quit'];
  beforeQuit();
  const efterFørste = stub.stopped.length;
  beforeQuit();
  assert.equal(stub.stopped.length, efterFørste, 'blokkerne må ikke stoppes igen, når de allerede er stoppet');
});
