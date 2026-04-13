const {
  app,
  BrowserWindow,
  Menu,
  shell,
  nativeImage,
  globalShortcut,
  nativeTheme,
} = require('electron');
const path = require('path');
const Store = require('electron-store');
const { createTray, destroyTray } = require('./tray');

// Set app name early (before app.whenReady)
app.setName('WikiHub');
if (process.platform === 'darwin') {
  process.title = 'WikiHub';
}

const isDev = !app.isPackaged;
const BASE_URL = isDev
  ? 'http://localhost:5100'
  : 'https://wikihub.globalbr.ai';

const store = new Store({
  defaults: {
    windowBounds: { width: 1200, height: 800 },
    windowPosition: null,
    windowMaximized: false,
  },
});

let mainWindow = null;

function createWindow() {
  const { width, height } = store.get('windowBounds');
  const position = store.get('windowPosition');
  const maximized = store.get('windowMaximized');

  const windowOptions = {
    width,
    height,
    title: 'WikiHub',
    titleBarStyle: 'hiddenInset',
    trafficLightPosition: { x: 15, y: 15 },
    backgroundColor: '#0f0e0c',
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
      // Persist cookies/session for auth
      partition: 'persist:wikihub',
    },
  };

  if (position) {
    windowOptions.x = position.x;
    windowOptions.y = position.y;
  }

  mainWindow = new BrowserWindow(windowOptions);

  if (maximized) {
    mainWindow.maximize();
  }

  // Set dock icon (macOS)
  if (process.platform === 'darwin') {
    const icon = nativeImage.createFromPath(
      path.join(__dirname, 'assets', 'icon.png')
    );
    if (!icon.isEmpty()) {
      app.dock.setIcon(icon);
    }
  }

  // Load the web app
  mainWindow.loadURL(BASE_URL);

  // Forward renderer console to terminal in dev mode
  if (isDev) {
    mainWindow.webContents.on('console-message', (event) => {
      const levels = ['LOG', 'WARN', 'ERROR'];
      console.log(`[Renderer ${levels[event.level] || 'LOG'}] ${event.message}`);
    });
  }

  // Save window state on changes
  const saveWindowState = () => {
    if (mainWindow.isDestroyed()) return;
    const bounds = mainWindow.getBounds();
    store.set('windowBounds', { width: bounds.width, height: bounds.height });
    store.set('windowPosition', { x: bounds.x, y: bounds.y });
    store.set('windowMaximized', mainWindow.isMaximized());
  };

  mainWindow.on('resize', saveWindowState);
  mainWindow.on('move', saveWindowState);
  mainWindow.on('maximize', saveWindowState);
  mainWindow.on('unmaximize', saveWindowState);

  // External links open in default browser
  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    if (!url.startsWith(BASE_URL)) {
      shell.openExternal(url);
      return { action: 'deny' };
    }
    return { action: 'allow' };
  });

  // Intercept navigation — keep wikihub in-app, external links in browser
  mainWindow.webContents.on('will-navigate', (event, url) => {
    if (!url.startsWith(BASE_URL) && !url.startsWith('about:')) {
      event.preventDefault();
      shell.openExternal(url);
    }
  });

  mainWindow.on('closed', () => {
    mainWindow = null;
  });

  // Create system tray
  createTray(mainWindow, BASE_URL);
}

function buildMenu() {
  const template = [
    {
      label: 'WikiHub',
      submenu: [
        { role: 'about', label: 'About WikiHub' },
        { type: 'separator' },
        {
          label: 'Settings',
          accelerator: 'CmdOrCtrl+,',
          click: () => {
            if (mainWindow) {
              mainWindow.webContents.loadURL(`${BASE_URL}/settings`);
            }
          },
        },
        { type: 'separator' },
        { role: 'services' },
        { type: 'separator' },
        { role: 'hide', label: 'Hide WikiHub' },
        { role: 'hideOthers' },
        { role: 'unhide' },
        { type: 'separator' },
        { role: 'quit', label: 'Quit WikiHub' },
      ],
    },
    {
      label: 'File',
      submenu: [
        {
          label: 'New Page',
          accelerator: 'CmdOrCtrl+N',
          click: () => {
            if (mainWindow) {
              // Trigger the web app's new page flow
              mainWindow.webContents.executeJavaScript(
                `document.querySelector('[data-action="new-page"]')?.click()`
              );
            }
          },
        },
        { type: 'separator' },
        { role: 'close' },
      ],
    },
    {
      label: 'Edit',
      submenu: [
        { role: 'undo' },
        { role: 'redo' },
        { type: 'separator' },
        { role: 'cut' },
        { role: 'copy' },
        { role: 'paste' },
        { role: 'pasteAndMatchStyle' },
        { role: 'selectAll' },
        { type: 'separator' },
        {
          label: 'Find',
          accelerator: 'CmdOrCtrl+F',
          click: () => {
            if (mainWindow) {
              mainWindow.webContents.executeJavaScript(
                `window.find && window.find()`
              );
            }
          },
        },
      ],
    },
    {
      label: 'View',
      submenu: [
        {
          label: 'Search',
          accelerator: 'CmdOrCtrl+K',
          click: () => {
            if (mainWindow) {
              triggerSearch();
            }
          },
        },
        { type: 'separator' },
        { role: 'reload' },
        { role: 'forceReload' },
        { role: 'toggleDevTools' },
        { type: 'separator' },
        { role: 'resetZoom' },
        { role: 'zoomIn' },
        { role: 'zoomOut' },
        { type: 'separator' },
        { role: 'togglefullscreen' },
      ],
    },
    {
      label: 'Go',
      submenu: [
        {
          label: 'Back',
          accelerator: 'CmdOrCtrl+[',
          click: () => {
            if (mainWindow?.webContents.canGoBack()) {
              mainWindow.webContents.goBack();
            }
          },
        },
        {
          label: 'Forward',
          accelerator: 'CmdOrCtrl+]',
          click: () => {
            if (mainWindow?.webContents.canGoForward()) {
              mainWindow.webContents.goForward();
            }
          },
        },
        { type: 'separator' },
        {
          label: 'Home',
          accelerator: 'CmdOrCtrl+Shift+H',
          click: () => {
            if (mainWindow) {
              mainWindow.webContents.loadURL(BASE_URL);
            }
          },
        },
      ],
    },
    {
      label: 'Window',
      submenu: [
        { role: 'minimize' },
        { role: 'zoom' },
        { type: 'separator' },
        { role: 'front' },
      ],
    },
    {
      label: 'Help',
      submenu: [
        {
          label: 'WikiHub Documentation',
          click: () => shell.openExternal('https://wikihub.globalbr.ai/agents'),
        },
        {
          label: 'Report an Issue',
          click: () =>
            shell.openExternal('https://github.com/tmad4000/wikihub/issues'),
        },
      ],
    },
  ];

  const menu = Menu.buildFromTemplate(template);
  Menu.setApplicationMenu(menu);
}

function triggerSearch() {
  if (!mainWindow) return;
  // Dispatch Cmd+K to the web app's search overlay
  mainWindow.webContents.executeJavaScript(`
    (() => {
      const event = new KeyboardEvent('keydown', {
        key: 'k',
        code: 'KeyK',
        metaKey: true,
        bubbles: true,
      });
      document.dispatchEvent(event);
    })();
  `);
}

// Dark/light mode: follow system preference
nativeTheme.on('updated', () => {
  if (mainWindow) {
    const isDark = nativeTheme.shouldUseDarkColors;
    mainWindow.webContents.executeJavaScript(
      `document.documentElement.setAttribute('data-theme', '${isDark ? 'dark' : 'light'}')`
    );
  }
});

app.whenReady().then(() => {
  buildMenu();
  createWindow();
});

app.on('window-all-closed', () => {
  destroyTray();
  if (process.platform !== 'darwin') {
    app.quit();
  }
});

app.on('activate', () => {
  if (mainWindow === null) {
    createWindow();
  } else {
    mainWindow.show();
    mainWindow.focus();
  }
});

app.on('will-quit', () => {
  destroyTray();
  globalShortcut.unregisterAll();
});
