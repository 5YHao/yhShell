// ZMODEM peer used by tests/test_zmodem_interop.py.
//
// Speaks the protocol over stdin/stdout (binary) using zmodem.js, which is an
// independent implementation, so the Python code in yhshell/zmodem.py is
// checked against something other than itself.
//
//   node zmodem_peer.mjs recv <directory>
//   node zmodem_peer.mjs send <file>
//
// Diagnostics go to stderr; stdout carries raw ZMODEM bytes only.

import fs from 'node:fs';
import path from 'node:path';
import { createRequire } from 'node:module';

const require = createRequire(import.meta.url);
const Zmodem = require('zmodem.js');

const mode = process.argv[2];
const target = process.argv[3];
if (!mode || !target || !['recv', 'send'].includes(mode)) {
  process.stderr.write('usage: zmodem_peer.mjs <recv|send> <path>\n');
  process.exit(2);
}
const role = mode === 'recv' ? 'receive' : 'send';

function finish(code, detail) {
  if (detail) process.stderr.write(String(detail) + '\n');
  process.stderr.write(`EXIT ${code}\n`);
  process.exit(code);
}

function sendOctets(octets) {
  process.stdout.write(Buffer.from(octets));
}

function writeHeader(name) {
  const header = Zmodem.Header.build(name);
  process.stdout.write(Buffer.from(header.to_hex()));
}

const sentry = new Zmodem.Sentry({
  to_terminal: () => {},
  sender: sendOctets,
  on_retract: () => {},
  on_detect: (detection) => {
    const detected = detection.get_session_role();
    if (detected !== role) {
      detection.deny();
      return;
    }
    const session = detection.confirm();
    session.on('session_end', () => finish(0));
    if (role === 'receive') {
      session.on('offer', (offer) => {
        const details = offer.get_details();
        const destination = path.join(target, path.basename(details.name));
        const stream = fs.createWriteStream(destination);
        let received = 0;
        offer.on('input', (octets) => {
          received += octets.length;
          stream.write(Buffer.from(octets));
        });
        offer.accept().then(() => {
          stream.end(() => {
            process.stderr.write(`FILE ${destination} ${received}\n`);
          });
        }).catch((err) => finish(5, err && err.stack));
      });
      session.start();
    } else {
      const stat = fs.statSync(target);
      session.send_offer({
        name: path.basename(target),
        size: stat.size,
        mode: 0o100644,
        mtime: Math.floor(stat.mtimeMs / 1000),
      }).then((transfer) => {
        if (!transfer) return finish(3, 'offer skipped');
        const buffer = Buffer.alloc(8192);
        let offset = 0;
        const file = fs.openSync(target, 'r');
        for (;;) {
          const read = fs.readSync(file, buffer, 0, buffer.length, offset);
          if (read <= 0) break;
          offset += read;
          transfer.send(Array.from(buffer.subarray(0, read)));
        }
        fs.closeSync(file);
        transfer.end().then(() => {
          session.close().catch((err) => finish(6, err && err.stack));
        }).catch((err) => finish(6, err && err.stack));
      }).catch((err) => finish(4, err && err.stack));
    }
  },
});

process.stdin.on('data', (chunk) => sentry.consume(chunk));
process.stdin.on('end', () => finish(0));

// Kick the exchange off: the sentry needs a header from the peer to detect a
// session, and either side may be the one that starts.
writeHeader('ZRQINIT');
