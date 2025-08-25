const http = require('http');
const SerialPort = require('serialport');
const { ReadlineParser } = require('@serialport/parser-readline');
const { Buffer } = require('buffer');

// --- Environment Variables ---
const UART_PORT = process.env.DEVICE_UART_PORT || '/dev/ttyUSB0';
const UART_BAUDRATE = parseInt(process.env.DEVICE_UART_BAUDRATE || '115200', 10);
const HTTP_HOST = process.env.DEVICE_SERVER_HOST || '0.0.0.0';
const HTTP_PORT = parseInt(process.env.DEVICE_SERVER_PORT || '8080', 10);

// --- Serial Port Initialization ---
const port = new SerialPort(UART_PORT, {
    baudRate: UART_BAUDRATE,
    autoOpen: false,
});
let serialReady = false;
let serialQueue = [];
let lastSerialError = null;

function openSerial() {
    if (!serialReady) {
        port.open((err) => {
            if (err) {
                lastSerialError = err;
                serialReady = false;
            } else {
                serialReady = true;
                lastSerialError = null;
            }
        });
    }
}
openSerial();

port.on('error', (err) => {
    lastSerialError = err;
    serialReady = false;
});
port.on('close', () => {
    serialReady = false;
    setTimeout(openSerial, 1000);
});

// --- Serial Communication Utilities ---
function sendSerialCommand(commandBuffer, responseTimeout = 1000) {
    return new Promise((resolve, reject) => {
        if (!serialReady) {
            openSerial();
            setTimeout(() => {
                if (!serialReady) return reject(new Error('Serial port not ready'));
                sendSerialCommand(commandBuffer, responseTimeout).then(resolve).catch(reject);
            }, 200);
            return;
        }
        let timer;
        let responseBuffers = [];
        const onData = (data) => responseBuffers.push(data);
        const onCloseOrError = (err) => {
            cleanup();
            reject(err || new Error('Serial port closed'));
        };
        function cleanup() {
            port.off('data', onData);
            port.off('close', onCloseOrError);
            port.off('error', onCloseOrError);
            clearTimeout(timer);
        }
        port.on('data', onData);
        port.once('close', onCloseOrError);
        port.once('error', onCloseOrError);
        port.write(commandBuffer, (err) => {
            if (err) {
                cleanup();
                reject(err);
            } else {
                timer = setTimeout(() => {
                    cleanup();
                    resolve(Buffer.concat(responseBuffers));
                }, responseTimeout);
            }
        });
    });
}

// --- Frame Protocol Utilities (Example for Custom Frame) ---
function buildFrame(cmd, payload) {
    // Example: [0xAA][LEN][CMD][...PAYLOAD][CHK]
    // LEN = CMD(1)+PAYLOAD(?)+CHK(1)
    payload = payload || Buffer.alloc(0);
    let len = 1 + payload.length + 1;
    let frame = Buffer.alloc(2 + payload.length + 1);
    frame[0] = 0xAA;
    frame[1] = len;
    frame[2] = cmd;
    if (payload.length > 0) payload.copy(frame, 3);
    let chk = 0;
    for (let i = 0; i < frame.length - 1; i++) chk += frame[i];
    frame[frame.length - 1] = chk & 0xFF;
    return frame;
}

function parseFrame(buf) {
    // Very simple parser for demo purposes. Real parsing may require more logic.
    if (!Buffer.isBuffer(buf) || buf.length < 4) return null;
    if (buf[0] !== 0xAA) return null;
    const len = buf[1];
    if (buf.length !== len + 1) return null;
    // Checksum
    let chk = 0;
    for (let i = 0; i < buf.length - 1; i++) chk += buf[i];
    if ((chk & 0xFF) !== buf[buf.length - 1]) return null;
    const cmd = buf[2];
    const payload = buf.slice(3, buf.length - 1);
    return { cmd, payload };
}

// --- Command Codes ---
// These are EXAMPLES; use correct codes for the real protocol!
const CMD_MAP = {
    STATUS: 0x01,
    HARDWARE: 0x02,
    PROGRAM: 0x03,
    CHASSIS: 0x04,
    REMOTE: 0x10,
    SPEED: 0x11,
    ESTOP: 0x12,
    FAULT: 0x13,
    DRIVER: 0x14,
    CHARGE: 0x15,
    LIGHT: 0x16,
    ODOCLEAR: 0x17
};

// --- API Handlers ---
async function handleStatus(req, res) {
    try {
        const frame = buildFrame(CMD_MAP.STATUS);
        const response = await sendSerialCommand(frame);
        const data = parseFrame(response);
        // Binary protocol to JSON mapping - EXAMPLE
        if (!data) throw new Error('Malformed response');
        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({
            speed: data.payload[0],
            wheel: data.payload[1],
            estop: data.payload[2] !== 0,
            fault: data.payload[3],
            motor: data.payload[4],
            temperature: data.payload.readInt16LE(5),
            voltage: data.payload.readUInt16LE(7)
        }));
    } catch (e) {
        res.writeHead(500, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ error: e.message }));
    }
}
async function handleHardware(req, res) {
    try {
        const frame = buildFrame(CMD_MAP.HARDWARE);
        const response = await sendSerialCommand(frame);
        const data = parseFrame(response);
        if (!data) throw new Error('Malformed response');
        res.writeHead(200, { 'Content-Type': 'application/json' });
        // Example: parse payload as needed
        res.end(JSON.stringify({ hardware: data.payload.toString('hex') }));
    } catch (e) {
        res.writeHead(500, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ error: e.message }));
    }
}
async function handleProgram(req, res) {
    try {
        const frame = buildFrame(CMD_MAP.PROGRAM);
        const response = await sendSerialCommand(frame);
        const data = parseFrame(response);
        if (!data) throw new Error('Malformed response');
        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ version: data.payload.toString() }));
    } catch (e) {
        res.writeHead(500, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ error: e.message }));
    }
}
async function handleChassis(req, res) {
    try {
        const frame = buildFrame(CMD_MAP.CHASSIS);
        const response = await sendSerialCommand(frame);
        const data = parseFrame(response);
        if (!data) throw new Error('Malformed response');
        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ chassis: data.payload.toString('hex') }));
    } catch (e) {
        res.writeHead(500, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ error: e.message }));
    }
}
async function handleRemote(req, res) {
    let body = [];
    req.on('data', chunk => body.push(chunk));
    req.on('end', async () => {
        try {
            body = Buffer.concat(body);
            // Pass the body as payload in the frame
            const frame = buildFrame(CMD_MAP.REMOTE, body);
            const response = await sendSerialCommand(frame);
            const data = parseFrame(response);
            res.writeHead(200, { 'Content-Type': 'application/json' });
            res.end(JSON.stringify({ ack: !!data }));
        } catch (e) {
            res.writeHead(500, { 'Content-Type': 'application/json' });
            res.end(JSON.stringify({ error: e.message }));
        }
    });
}
async function handleSpeed(req, res) {
    let body = [];
    req.on('data', chunk => body.push(chunk));
    req.on('end', async () => {
        try {
            body = Buffer.concat(body);
            const frame = buildFrame(CMD_MAP.SPEED, body);
            const response = await sendSerialCommand(frame);
            const data = parseFrame(response);
            res.writeHead(200, { 'Content-Type': 'application/json' });
            res.end(JSON.stringify({ ack: !!data }));
        } catch (e) {
            res.writeHead(500, { 'Content-Type': 'application/json' });
            res.end(JSON.stringify({ error: e.message }));
        }
    });
}
async function handleEstop(req, res) {
    try {
        const frame = buildFrame(CMD_MAP.ESTOP);
        const response = await sendSerialCommand(frame);
        const data = parseFrame(response);
        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ ack: !!data }));
    } catch (e) {
        res.writeHead(500, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ error: e.message }));
    }
}
async function handleFault(req, res) {
    try {
        const frame = buildFrame(CMD_MAP.FAULT);
        const response = await sendSerialCommand(frame);
        const data = parseFrame(response);
        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ ack: !!data }));
    } catch (e) {
        res.writeHead(500, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ error: e.message }));
    }
}
async function handleDriver(req, res) {
    let body = [];
    req.on('data', chunk => body.push(chunk));
    req.on('end', async () => {
        try {
            body = Buffer.concat(body);
            const frame = buildFrame(CMD_MAP.DRIVER, body);
            const response = await sendSerialCommand(frame);
            const data = parseFrame(response);
            res.writeHead(200, { 'Content-Type': 'application/json' });
            res.end(JSON.stringify({ ack: !!data }));
        } catch (e) {
            res.writeHead(500, { 'Content-Type': 'application/json' });
            res.end(JSON.stringify({ error: e.message }));
        }
    });
}
async function handleCharge(req, res) {
    let body = [];
    req.on('data', chunk => body.push(chunk));
    req.on('end', async () => {
        try {
            body = Buffer.concat(body);
            const frame = buildFrame(CMD_MAP.CHARGE, body);
            const response = await sendSerialCommand(frame);
            const data = parseFrame(response);
            res.writeHead(200, { 'Content-Type': 'application/json' });
            res.end(JSON.stringify({ ack: !!data }));
        } catch (e) {
            res.writeHead(500, { 'Content-Type': 'application/json' });
            res.end(JSON.stringify({ error: e.message }));
        }
    });
}
async function handleLight(req, res) {
    let body = [];
    req.on('data', chunk => body.push(chunk));
    req.on('end', async () => {
        try {
            body = Buffer.concat(body);
            const frame = buildFrame(CMD_MAP.LIGHT, body);
            const response = await sendSerialCommand(frame);
            const data = parseFrame(response);
            res.writeHead(200, { 'Content-Type': 'application/json' });
            res.end(JSON.stringify({ ack: !!data }));
        } catch (e) {
            res.writeHead(500, { 'Content-Type': 'application/json' });
            res.end(JSON.stringify({ error: e.message }));
        }
    });
}
async function handleOdoclear(req, res) {
    try {
        const frame = buildFrame(CMD_MAP.ODOCLEAR);
        const response = await sendSerialCommand(frame);
        const data = parseFrame(response);
        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ ack: !!data }));
    } catch (e) {
        res.writeHead(500, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ error: e.message }));
    }
}

// --- HTTP Server Routing ---
const ROUTES = [
    { method: 'GET', path: /^\/status$/, handler: handleStatus },
    { method: 'GET', path: /^\/hardware$/, handler: handleHardware },
    { method: 'GET', path: /^\/program$/, handler: handleProgram },
    { method: 'GET', path: /^\/chassis$/, handler: handleChassis },
    { method: 'POST', path: /^\/remote$/, handler: handleRemote },
    { method: 'POST', path: /^\/speed$/, handler: handleSpeed },
    { method: 'POST', path: /^\/estop$/, handler: handleEstop },
    { method: 'POST', path: /^\/fault$/, handler: handleFault },
    { method: 'POST', path: /^\/driver$/, handler: handleDriver },
    { method: 'POST', path: /^\/charge$/, handler: handleCharge },
    { method: 'POST', path: /^\/light$/, handler: handleLight },
    { method: 'POST', path: /^\/odoclear$/, handler: handleOdoclear }
];

const server = http.createServer((req, res) => {
    // CORS preflight
    if (req.method === 'OPTIONS') {
        res.writeHead(204, {
            'Access-Control-Allow-Origin': '*',
            'Access-Control-Allow-Methods': 'GET,POST,OPTIONS',
            'Access-Control-Allow-Headers': 'Content-Type'
        });
        res.end();
        return;
    }
    res.setHeader('Access-Control-Allow-Origin', '*');
    const route = ROUTES.find(
        r => r.method === req.method && r.path.test(req.url.split('?')[0])
    );
    if (!route) {
        res.writeHead(404, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ error: 'Not found' }));
        return;
    }
    route.handler(req, res);
});

// --- Start Server ---
server.listen(HTTP_PORT, HTTP_HOST, () => {
    console.log(`SeaRobot HTTP Driver listening on http://${HTTP_HOST}:${HTTP_PORT}/`);
    console.log(`UART port: ${UART_PORT} @ ${UART_BAUDRATE}`);
});