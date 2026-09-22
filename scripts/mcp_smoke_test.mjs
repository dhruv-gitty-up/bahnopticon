import {spawn} from 'node:child_process';

const [, , label, command, ...args] = process.argv;
if (!label || !command) {
  console.error('usage: node scripts/mcp_smoke_test.mjs <label> <command> [args...]');
  process.exit(64);
}

const timeoutMs = Number(process.env.MCP_SMOKE_TIMEOUT_MS || 90_000);
const toolName = process.env.MCP_SMOKE_TOOL || '';
const toolArgs = process.env.MCP_SMOKE_ARGS
  ? JSON.parse(process.env.MCP_SMOKE_ARGS)
  : {};

const child = spawn(command, args, {
  cwd: process.cwd(),
  env: process.env,
  stdio: ['pipe', 'pipe', 'pipe'],
});

let stdoutBuffer = '';
let stderrBuffer = '';
let settled = false;
let initialized = false;

const finish = (error) => {
  if (settled) return;
  settled = true;
  clearTimeout(timer);
  if (child.exitCode === null) child.kill('SIGTERM');
  if (error) {
    console.error(`[FAIL] ${label}: ${error.message}`);
    if (stderrBuffer.trim()) console.error(stderrBuffer.trim().slice(-2000));
    process.exitCode = 1;
  } else {
    console.log(`[OK] ${label}: MCP initialize, tools/list${toolName ? `, and ${toolName}` : ''}`);
  }
};

const send = (message) => child.stdin.write(`${JSON.stringify(message)}\n`);

const handleMessage = (message) => {
  if (message.id === 1) {
    if (message.error) return finish(new Error(`initialize failed: ${JSON.stringify(message.error)}`));
    initialized = true;
    send({jsonrpc: '2.0', method: 'notifications/initialized'});
    send({jsonrpc: '2.0', id: 2, method: 'tools/list', params: {}});
    return;
  }
  if (message.id === 2) {
    if (message.error) return finish(new Error(`tools/list failed: ${JSON.stringify(message.error)}`));
    if (!Array.isArray(message.result?.tools)) return finish(new Error('tools/list returned no tools array'));
    if (!toolName) return finish();
    if (!message.result.tools.some(tool => tool.name === toolName)) {
      return finish(new Error(`tool ${toolName} is not advertised`));
    }
    send({
      jsonrpc: '2.0',
      id: 3,
      method: 'tools/call',
      params: {name: toolName, arguments: toolArgs},
    });
    return;
  }
  if (message.id === 3) {
    if (message.error) return finish(new Error(`tool call failed: ${JSON.stringify(message.error)}`));
    if (message.result?.isError) return finish(new Error(`tool returned an error: ${JSON.stringify(message.result.content)}`));
    finish();
  }
};

child.stdout.setEncoding('utf8');
child.stdout.on('data', chunk => {
  stdoutBuffer += chunk;
  for (;;) {
    const newline = stdoutBuffer.indexOf('\n');
    if (newline < 0) break;
    const line = stdoutBuffer.slice(0, newline).trim();
    stdoutBuffer = stdoutBuffer.slice(newline + 1);
    if (!line) continue;
    try {
      handleMessage(JSON.parse(line));
    } catch {
      // Some launchers emit progress text. MCP JSON responses are handled once
      // they arrive; non-JSON stdout is included if the process times out.
    }
  }
});
child.stderr.setEncoding('utf8');
child.stderr.on('data', chunk => { stderrBuffer += chunk; });
child.on('error', error => finish(error));
child.on('exit', code => {
  if (!settled) {
    finish(new Error(`process exited with status ${code}${initialized ? ' after initialize' : ''}`));
  }
});

const timer = setTimeout(() => {
  const output = stdoutBuffer.trim();
  finish(new Error(`timed out after ${timeoutMs}ms${output ? `; stdout: ${output.slice(-1000)}` : ''}`));
}, timeoutMs);

send({
  jsonrpc: '2.0',
  id: 1,
  method: 'initialize',
  params: {
    protocolVersion: '2024-11-05',
    capabilities: {},
    clientInfo: {name: 'bahnopticon-mcp-smoke-test', version: '1.0.0'},
  },
});
