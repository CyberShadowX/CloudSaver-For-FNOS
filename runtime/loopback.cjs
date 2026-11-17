'use strict';

// Constrain only this application's configured backend listener. Outbound
// connections and unrelated listeners keep Node's ordinary behavior.
const net = require('node:net');
const port = Number(process.env.CLOUDSAVER_BACKEND_PORT);
if (!Number.isInteger(port) || port < 1 || port > 65535) {
  throw new Error('A valid native backend port is required');
}

// Explicit musl-loader startup makes Node report the loader as execPath. Its
// private launcher restores fork/execFile self-starts without a system Node.
const launcherSetting = process.env.CLOUDSAVER_NODE_LAUNCHER;
if (launcherSetting !== undefined) {
  try {
    const fs = require('node:fs');
    const path = require('node:path');
    if (!path.isAbsolute(launcherSetting)) throw new Error();
    const launcher = path.resolve(launcherSetting);
    const info = fs.lstatSync(launcher);
    if (!info.isFile() || info.isSymbolicLink()
        || path.relative(fs.realpathSync(launcher), launcher) !== '') throw new Error();
    fs.accessSync(launcher, fs.constants.R_OK | (process.platform === 'win32' ? 0 : fs.constants.X_OK));
    process.execPath = launcher;
  } catch {
    throw new Error('A valid private native Node launcher is required');
  }
}
const marker = Symbol.for('cloudsaver.native.loopback');
if (!net.Server.prototype[marker]) {
  const original = net.Server.prototype.listen;
  const isTarget = value => (
    (typeof value === 'number' || (typeof value === 'string' && /^\d+$/.test(value)))
    && Number(value) === port
  );
  net.Server.prototype.listen = function (...args) {
    if (args[0] && typeof args[0] === 'object' && isTarget(args[0].port)) {
      args[0] = { ...args[0], host: '127.0.0.1' };
    } else if (isTarget(args[0])) {
      if (typeof args[1] === 'string') args[1] = '127.0.0.1';
      else args.splice(1, 0, '127.0.0.1');
    }
    return Reflect.apply(original, this, args);
  };
  Object.defineProperty(net.Server.prototype, marker, { value: true });
}
