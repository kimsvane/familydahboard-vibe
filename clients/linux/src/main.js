'use strict';

const fs = require('node:fs');
const path = require('node:path');
const { app, BrowserWindow, ipcMain, powerSaveBlocker, screen, session } = require('electron');
const { checkServer } = require('./connection');
const { readSettings, writeSettings } = require('./settings');
const { normalizeServerUrl, originOf } = require('./url');

const uiDirectory = path.join(__dirname, '..', 'ui');
let mainWindow = null;
let controlWindow = null;
let handleWindow = null;
let settings = null;
let configurationError = '';
let mode = 'starting';
let retryTimer = null;
let powerBlockerId = null;
let isQuitting = false;

app.setName('family-dashboard-kiosk');
const userDataDirectoryPath = path.join(app.getPath('appData'), 'family-dashboard-kiosk');
fs.mkdirSync(userDataDirectoryPath, { recursive: true, mode: 0o700 });
app.setPath('userData', userDataDirectoryPath);

function userDataDirectory() {
  return app.getPath('userData');
}

function loadConfiguration() {
  try {
    settings = readSettings(userDataDirectory());
    configurationError = '';
  } catch (error) {
    settings = null;
    configurationError = error.message;
  }
}

function stateForRenderer() {
  return {
    configured: Boolean(settings),
    serverUrl: settings?.serverUrl || '',
    configurationError,
    version: app.getVersion(),
  };
}

function clearRetry() {
  if (retryTimer) {
    clearTimeout(retryTimer);
    retryTimer = null;
  }
}

function positionHandle() {
  if (!handleWindow || handleWindow.isDestroyed() || !mainWindow || mainWindow.isDestroyed()) {
    return;
  }
  const display = screen.getDisplayMatching(mainWindow.getBounds());
  const area = display.workArea;
  const size = handleWindow.getBounds();
  const margin = 6;
  handleWindow.setBounds({
    x: Math.max(area.x, area.x + area.width - size.width - margin),
    y: Math.max(area.y, area.y + area.height - size.height - margin),
    width: size.width,
    height: size.height,
  });
  handleWindow.setAlwaysOnTop(true, 'screen-saver');
}

function showHandle() {
  if (!handleWindow || handleWindow.isDestroyed() || !settings || mode !== 'remote') {
    return;
  }
  positionHandle();
  if (!handleWindow.isVisible()) {
    handleWindow.showInactive();
  }
}

function hideHandle() {
  if (handleWindow && !handleWindow.isDestroyed() && handleWindow.isVisible()) {
    handleWindow.hide();
  }
}

function createHandleWindow() {
  handleWindow = new BrowserWindow({
    width: 68,
    height: 68,
    show: false,
    frame: false,
    transparent: true,
    backgroundColor: '#00000000',
    hasShadow: false,
    resizable: false,
    maximizable: false,
    minimizable: false,
    fullscreenable: false,
    skipTaskbar: true,
    alwaysOnTop: true,
    title: 'Family Dashboard indstillinger',
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
  });
  handleWindow.setAlwaysOnTop(true, 'screen-saver');
  handleWindow.loadFile(path.join(uiDirectory, 'handle.html'));
  handleWindow.once('ready-to-show', () => {
    if (mode === 'remote' && settings) {
      showHandle();
    }
  });
  handleWindow.on('closed', () => {
    handleWindow = null;
  });
}

function openControlWindow() {
  if (controlWindow && !controlWindow.isDestroyed()) {
    controlWindow.show();
    controlWindow.focus();
    return;
  }
  controlWindow = new BrowserWindow({
    width: 520,
    height: 640,
    show: false,
    parent: mainWindow || undefined,
    resizable: false,
    maximizable: false,
    fullscreenable: false,
    autoHideMenuBar: true,
    backgroundColor: '#0b1020',
    title: 'Family Dashboard indstillinger',
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
  });
  hideHandle();
  controlWindow.loadFile(path.join(uiDirectory, 'control.html'));
  controlWindow.once('ready-to-show', () => {
    controlWindow?.show();
    controlWindow?.focus();
  });
  controlWindow.on('closed', () => {
    controlWindow = null;
    if (mode === 'remote' && settings) {
      showHandle();
    }
  });
}

function hardenWebContents(webContents) {
  webContents.setWindowOpenHandler(() => ({ action: 'deny' }));
  webContents.on('will-attach-webview', (event) => event.preventDefault());
  webContents.on('will-navigate', (event, target) => {
    if (target.startsWith('file:')) {
      return;
    }
    if (settings) {
      try {
        if (originOf(target) === originOf(settings.serverUrl)) {
          return;
        }
      } catch {
        event.preventDefault();
        return;
      }
    }
    event.preventDefault();
  });
}

function createMainWindow() {
  mainWindow = new BrowserWindow({
    width: 1280,
    height: 800,
    minWidth: 800,
    minHeight: 600,
    show: false,
    frame: false,
    fullscreen: true,
    kiosk: true,
    autoHideMenuBar: true,
    backgroundColor: '#0b1020',
    title: 'Family Dashboard',
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      webSecurity: true,
      spellcheck: false,
    },
  });
  hardenWebContents(mainWindow.webContents);
  mainWindow.setFullScreen(true);
  mainWindow.setKiosk(true);
  mainWindow.once('ready-to-show', () => {
    mainWindow?.show();
    mainWindow?.setFullScreen(true);
    mainWindow?.setKiosk(true);
  });
  mainWindow.on('close', (event) => {
    if (!isQuitting) {
      event.preventDefault();
      mainWindow?.hide();
    }
  });
  mainWindow.on('closed', () => {
    mainWindow = null;
  });
  mainWindow.on('enter-full-screen', positionHandle);
  mainWindow.webContents.on('did-finish-load', () => {
    if (mode === 'remote') {
      showHandle();
    }
  });
  mainWindow.webContents.on('did-fail-load', (_event, code, _description, validatedUrl, isMainFrame) => {
    if (!isMainFrame || code === -3 || validatedUrl.startsWith('file:')) {
      return;
    }
    showOffline();
  });
  mainWindow.webContents.on('render-process-gone', () => {
    if (!isQuitting) {
      clearRetry();
      retryTimer = setTimeout(() => {
        if (settings) {
          loadDashboard();
        } else {
          loadSetup();
        }
      }, 2000);
    }
  });
}

async function loadSetup() {
  clearRetry();
  mode = 'setup';
  hideHandle();
  if (mainWindow && !mainWindow.isDestroyed()) {
    await mainWindow.loadFile(path.join(uiDirectory, 'setup.html'));
    mainWindow.show();
    mainWindow.setKiosk(true);
  }
}

async function loadDashboard() {
  if (!settings) {
    await loadSetup();
    return;
  }
  clearRetry();
  mode = 'remote';
  hideHandle();
  if (!mainWindow || mainWindow.isDestroyed()) {
    return;
  }
  try {
    await mainWindow.loadURL(settings.serverUrl);
  } catch {
    showOffline();
  }
}

function showOffline() {
  if (isQuitting || !settings || !mainWindow || mainWindow.isDestroyed()) {
    return;
  }
  clearRetry();
  mode = 'offline';
  hideHandle();
  mainWindow.loadFile(path.join(uiDirectory, 'offline.html')).catch(() => {});
  retryTimer = setTimeout(() => loadDashboard(), 15000);
}

function registerIpcHandlers() {
  ipcMain.handle('client:get-state', () => stateForRenderer());
  ipcMain.handle('client:test-connection', async (_event, value) => {
    try {
      return await checkServer(normalizeServerUrl(value?.serverUrl));
    } catch (error) {
      return { ok: false, message: error.message };
    }
  });
  ipcMain.handle('client:save-config', async (_event, value) => {
    settings = writeSettings(userDataDirectory(), value);
    configurationError = '';
    await loadDashboard();
    return stateForRenderer();
  });
  ipcMain.handle('client:reconnect', () => loadDashboard());
  ipcMain.handle('client:open-control', () => openControlWindow());
  ipcMain.handle('client:close-control', () => {
    if (controlWindow && !controlWindow.isDestroyed()) {
      controlWindow.close();
    }
  });
  ipcMain.handle('client:quit', () => {
    isQuitting = true;
    app.quit();
  });
}

async function start() {
  await app.whenReady();
  loadConfiguration();
  session.defaultSession.setPermissionRequestHandler((_webContents, _permission, callback) => callback(false));
  session.defaultSession.setPermissionCheckHandler(() => false);
  registerIpcHandlers();
  createMainWindow();
  createHandleWindow();
  powerBlockerId = powerSaveBlocker.start('prevent-display-sleep');
  screen.on('display-metrics-changed', positionHandle);
  if (settings) {
    await loadDashboard();
  } else {
    await loadSetup();
  }
}

if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  app.on('second-instance', () => {
    if (mainWindow && !mainWindow.isDestroyed()) {
      mainWindow.show();
      mainWindow.focus();
      positionHandle();
    }
  });
  app.on('before-quit', () => {
    isQuitting = true;
    clearRetry();
    if (powerBlockerId !== null) {
      powerSaveBlocker.stop(powerBlockerId);
      powerBlockerId = null;
    }
  });
  app.on('window-all-closed', () => app.quit());
  app.on('activate', () => {
    if (mainWindow && !mainWindow.isDestroyed()) {
      mainWindow.show();
    }
  });
  start().catch((error) => {
    configurationError = error.message;
    loadSetup();
  });
}
