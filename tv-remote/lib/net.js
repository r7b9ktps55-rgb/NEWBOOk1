'use strict';

const net = require('net');
const os = require('os');
const dgram = require('dgram');

/** IPv4-адреса сервера в локальной сети. */
function localIPv4() {
  const out = [];
  for (const list of Object.values(os.networkInterfaces())) {
    for (const a of list || []) {
      if (a.family === 'IPv4' && !a.internal) out.push({ address: a.address, netmask: a.netmask });
    }
  }
  return out;
}

function probe(host, port, timeout = 400) {
  return new Promise((resolve) => {
    const sock = net.connect({ host, port });
    const done = (ok) => {
      sock.destroy();
      resolve(ok);
    };
    sock.setTimeout(timeout, () => done(false));
    sock.once('connect', () => done(true));
    sock.once('error', () => done(false));
  });
}

/** Ищет в /24-подсетях сервера устройства с открытым портом ADB (5555). */
async function discover(port = 5555) {
  const hosts = [];
  for (const { address } of localIPv4()) {
    const base = address.split('.').slice(0, 3).join('.');
    for (let i = 1; i < 255; i++) {
      const h = `${base}.${i}`;
      if (h !== address) hosts.push(h);
    }
  }
  const found = [];
  const queue = [...new Set(hosts)];
  const workers = Array.from({ length: 64 }, async () => {
    while (queue.length) {
      const h = queue.shift();
      if (await probe(h, port)) found.push(h);
    }
  });
  await Promise.all(workers);
  return found.sort((a, b) => a.localeCompare(b, undefined, { numeric: true }));
}

/** Wake-on-LAN magic packet. */
function wakeOnLan(mac) {
  const clean = String(mac || '').replace(/[^0-9a-f]/gi, '');
  if (clean.length !== 12) return Promise.reject(new Error('Неверный MAC-адрес'));
  const macBuf = Buffer.from(clean, 'hex');
  const pkt = Buffer.alloc(102, 0xff);
  for (let i = 0; i < 16; i++) macBuf.copy(pkt, 6 + i * 6);
  const targets = new Set(['255.255.255.255']);
  for (const { address, netmask } of localIPv4()) {
    const a = address.split('.').map(Number);
    const m = netmask.split('.').map(Number);
    targets.add(a.map((x, i) => (x | (~m[i] & 255))).join('.'));
  }
  return new Promise((resolve) => {
    const sock = dgram.createSocket('udp4');
    sock.once('error', () => resolve());
    sock.bind(() => {
      sock.setBroadcast(true);
      let left = targets.size * 2;
      const fin = () => {
        if (--left <= 0) {
          sock.close();
          resolve();
        }
      };
      for (const t of targets) {
        sock.send(pkt, 9, t, fin);
        sock.send(pkt, 7, t, fin);
      }
    });
  });
}

module.exports = { localIPv4, discover, wakeOnLan, probe };
