const { Tray, Menu, nativeImage } = require('electron');
const path = require('path');

let tray = null;

function createTray(mainWindow, baseURL) {
  // Use a 22x22 template image for macOS menu bar
  const iconPath = path.join(__dirname, 'assets', 'icon.png');
  let icon = nativeImage.createFromPath(iconPath);
  icon = icon.resize({ width: 22, height: 22 });
  icon.setTemplateImage(true);

  tray = new Tray(icon);
  tray.setToolTip('WikiHub');

  const contextMenu = Menu.buildFromTemplate([
    {
      label: 'Open WikiHub',
      click: () => {
        mainWindow.show();
        mainWindow.focus();
      },
    },
    { type: 'separator' },
    {
      label: 'Search (Cmd+K)',
      accelerator: 'CmdOrCtrl+K',
      click: () => {
        mainWindow.show();
        mainWindow.focus();
        mainWindow.webContents.send('trigger-search');
      },
    },
    {
      label: 'New Page',
      click: () => {
        mainWindow.show();
        mainWindow.focus();
        // Navigate to new page — the web app handles the UI
        mainWindow.webContents.loadURL(baseURL);
      },
    },
    { type: 'separator' },
    {
      label: 'Quit WikiHub',
      accelerator: 'CmdOrCtrl+Q',
      role: 'quit',
    },
  ]);

  tray.setContextMenu(contextMenu);

  tray.on('click', () => {
    mainWindow.show();
    mainWindow.focus();
  });

  return tray;
}

function destroyTray() {
  if (tray) {
    tray.destroy();
    tray = null;
  }
}

module.exports = { createTray, destroyTray };
