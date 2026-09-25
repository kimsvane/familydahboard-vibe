'use strict';

const { contextBridge, ipcRenderer } = require('electron');

const isLocalUi = window.location.protocol === 'file:' && window.location.pathname.includes('/ui/');

if (isLocalUi) {
  contextBridge.exposeInMainWorld('familyDashboard', {
    getState: () => ipcRenderer.invoke('client:get-state'),
    saveConfig: (value) => ipcRenderer.invoke('client:save-config', value),
    testConnection: (value) => ipcRenderer.invoke('client:test-connection', value),
    reconnect: () => ipcRenderer.invoke('client:reconnect'),
    openControl: () => ipcRenderer.invoke('client:open-control'),
    closeControl: () => ipcRenderer.invoke('client:close-control'),
    quit: () => ipcRenderer.invoke('client:quit'),
  });
}
