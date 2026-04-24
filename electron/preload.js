const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('wikihub', {
  platform: process.platform,
  onNavigate: (callback) => ipcRenderer.on('navigate', (_e, url) => callback(url)),
  onTriggerSearch: (callback) => ipcRenderer.on('trigger-search', () => callback()),
});
