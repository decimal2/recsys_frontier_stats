// Dependency-free Chrome DevTools Protocol support for the rendering audit.
import {spawn} from 'node:child_process';
import {createServer} from 'node:http';
import {createReadStream, createWriteStream, existsSync, mkdirSync, mkdtempSync, realpathSync, rmSync, statSync, writeFileSync} from 'node:fs';
import {basename, delimiter, extname, join, resolve, sep} from 'node:path';
import {setTimeout as delay} from 'node:timers/promises';

export function findChrome() {
  if (process.env.CHROME_BIN) return process.env.CHROME_BIN;
  for (const directory of (process.env.PATH || '').split(delimiter)) {
    for (const name of ['google-chrome', 'google-chrome-stable', 'chromium', 'chromium-browser']) {
      const candidate = join(directory, name);
      if (existsSync(candidate)) return candidate;
    }
  }
  throw new Error('Chrome not found. Set CHROME_BIN to an installed Chrome executable.');
}

export async function serveSite(directory) {
  const root = realpathSync(directory);
  const types = {'.html':'text/html; charset=utf-8', '.json':'application/json', '.js':'text/javascript',
    '.css':'text/css', '.svg':'image/svg+xml', '.png':'image/png', '.jpg':'image/jpeg',
    '.jpeg':'image/jpeg', '.webp':'image/webp', '.woff2':'font/woff2'};
  const server = createServer((request, response) => {
    try {
      const pathname = decodeURIComponent(new URL(request.url, 'http://localhost').pathname);
      let file = resolve(root, '.' + pathname);
      if (file !== root && !file.startsWith(root + sep)) throw new Error('Outside site');
      if (statSync(file).isDirectory()) file = join(file, 'index.html');
      file = realpathSync(file);
      if (!file.startsWith(root + sep)) throw new Error('Outside site');
      response.writeHead(200, {'Content-Type': types[extname(file)] || 'application/octet-stream', 'Cache-Control':'no-store'});
      createReadStream(file).on('error', () => response.destroy()).pipe(response);
    } catch {
      response.writeHead(404, {'Content-Type':'text/plain'});
      response.end('Not found');
    }
  });
  await new Promise((accept, reject) => {
    server.once('error', reject);
    server.listen(0, '127.0.0.1', accept);
  });
  return {url:`http://127.0.0.1:${server.address().port}`, close:async () => {
    server.closeAllConnections();
    await new Promise(accept => server.close(accept));
  }};
}

export class Browser {
  constructor({outputDir, executable = findChrome()} = {}) {
    this.outputDir = resolve(outputDir);
    mkdirSync(this.outputDir, {recursive:true});
    this.profile = mkdtempSync(join(this.outputDir, 'chrome-profile-'));
    this.pending = new Map(); this.events = []; this.sequence = 0; this.buffer = '';
    this.stderr = createWriteStream(join(this.outputDir, 'chrome-stderr.log'));
    // CI reads only our local static site. No user profile, extensions or fonts config.
    this.proc = spawn(executable, ['--headless=new', '--no-sandbox', '--disable-dev-shm-usage',
      '--no-first-run', '--no-default-browser-check', '--disable-background-networking',
      '--disable-component-update', '--disable-sync', '--remote-debugging-pipe',
      `--user-data-dir=${this.profile}`, 'about:blank'],
    {stdio:['ignore', 'ignore', 'pipe', 'pipe', 'pipe']});
    this.proc.stderr.pipe(this.stderr);
    this.proc.once('error', error => this.fail(error));
    this.proc.once('exit', (code, signal) => this.fail(new Error(`Chrome exited (${code ?? signal}); see chrome-stderr.log`)));
    this.proc.stdio[3].on('error', error => this.fail(error));
    this.proc.stdio[4].on('error', error => this.fail(error));
    this.proc.stdio[4].setEncoding('utf8');
    this.proc.stdio[4].on('data', chunk => {
      this.buffer += chunk;
      let end;
      while ((end = this.buffer.indexOf('\0')) >= 0) {
        const raw = this.buffer.slice(0, end); this.buffer = this.buffer.slice(end + 1);
        if (!raw) continue;
        let message;
        try { message = JSON.parse(raw); } catch (error) { this.fail(error); continue; }
        const pending = this.pending.get(message.id);
        if (!pending) {
          if (message.method === 'Runtime.exceptionThrown') this.events.push(message);
          continue;
        }
        this.pending.delete(message.id); clearTimeout(pending.timer);
        if (message.error) pending.reject(new Error(JSON.stringify(message.error)));
        else pending.resolve(message.result);
      }
    });
  }

  fail(error) {
    this.failure = error;
    for (const pending of this.pending.values()) {
      clearTimeout(pending.timer); pending.reject(error);
    }
    this.pending.clear();
  }

  call(method, params = {}, sessionId) {
    if (this.failure) return Promise.reject(this.failure);
    const id = ++this.sequence;
    return new Promise((accept, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id); reject(new Error(`CDP timeout: ${method}`));
      }, 45000);
      this.pending.set(id, {resolve:accept, reject, timer});
      this.proc.stdio[3].write(JSON.stringify({id, method, params, ...(sessionId ? {sessionId} : {})}) + '\0',
        error => { if (error) this.fail(error); });
    });
  }

  async page(url, {width = 1440, height = 1100, dark = true, beforeLoad = ''} = {}) {
    const {targetId} = await this.call('Target.createTarget', {url:'about:blank'});
    const {sessionId} = await this.call('Target.attachToTarget', {targetId, flatten:true});
    const page = new Page(this, targetId, sessionId);
    try {
      await page.call('Page.enable'); await page.call('Runtime.enable');
      await page.resize(width, height);
      await page.call('Emulation.setEmulatedMedia', {features:[
        {name:'prefers-color-scheme', value:dark ? 'dark' : 'light'},
        {name:'prefers-reduced-motion', value:'reduce'}]});
      if (beforeLoad) await page.call('Page.addScriptToEvaluateOnNewDocument', {source:beforeLoad});
      if (url) await page.navigate(url);
      return page;
    } catch (error) { await page.close().catch(() => {}); throw error; }
  }

  async close() {
    if (!this.failure) await this.call('Browser.close').catch(() => {});
    if (this.proc.exitCode === null && this.proc.signalCode === null) {
      await Promise.race([new Promise(accept => this.proc.once('exit', accept)), delay(2000)]);
      if (this.proc.exitCode === null && this.proc.signalCode === null) this.proc.kill('SIGKILL');
    }
    this.stderr.end();
    rmSync(this.profile, {recursive:true, force:true, maxRetries:3});
  }
}

export class Page {
  constructor(browser, target, session) { this.browser = browser; this.target = target; this.session = session; }
  call(method, params = {}) { return this.browser.call(method, params, this.session); }
  async eval(expression, awaitPromise = true) {
    const result = await this.call('Runtime.evaluate', {expression, returnByValue:true, awaitPromise});
    if (result.exceptionDetails) throw new Error(JSON.stringify(result.exceptionDetails));
    return result.result.value;
  }
  async resize(width, height = 1100) {
    this.width = width; this.height = height;
    await this.call('Emulation.setDeviceMetricsOverride', {width, height, deviceScaleFactor:1, mobile:false});
  }
  async settle() {
    await this.eval(`(async()=>{await document.fonts.ready;for(let i=0;i<4;i++)await new Promise(r=>requestAnimationFrame(r));})()`);
  }
  async navigate(url) {
    const result = await this.call('Page.navigate', {url});
    if (result.errorText) throw new Error(`Navigation failed: ${result.errorText}`);
    let loaded = false;
    for (let attempt = 0; attempt < 200; attempt++) {
      loaded = await this.eval(`location.href === ${JSON.stringify(url)} && document.readyState === 'complete'`);
      if (loaded) break;
      await delay(100);
    }
    if (!loaded) throw new Error(`Page load timed out: ${url}`);
    this.imageErrors = await this.eval(`(async()=>{
      const images=[...document.images],errors=[];
      images.forEach(image=>image.loading='eager');
      await Promise.all(images.map(async image=>{
        let timer;
        try {
          await Promise.race([image.decode(),new Promise((_,reject)=>{timer=setTimeout(()=>reject(Error('image decode timed out')),30000);})]);
          if(!image.complete||!image.naturalWidth)throw Error('image has no decoded pixels');
        } catch(error) {errors.push({src:image.currentSrc||image.src,error:String(error)});}
        finally {clearTimeout(timer);}
      }));
      return errors;
    })()`);
    await this.settle();
  }
  async setContent(html) {
    const {frameTree} = await this.call('Page.getFrameTree');
    await this.call('Page.setDocumentContent', {frameId:frameTree.frame.id, html});
    await this.settle();
  }
  async screenshot(file, selector = null, maxHeight = 2000) {
    mkdirSync(resolve(file, '..'), {recursive:true});
    let clip;
    if (selector) {
      clip = await this.eval(`(()=>{const e=document.querySelector(${JSON.stringify(selector)});if(!e)throw Error('Screenshot target missing');
        e.scrollIntoView({block:'start',behavior:'instant'});const r=e.getBoundingClientRect(),y=Math.max(0,r.top+scrollY-160);
        return {x:0,y,width:innerWidth,height:Math.max(1,Math.min(${maxHeight},r.height+320,document.documentElement.scrollHeight-y)),scale:1};})()`);
    }
    const result = await this.call('Page.captureScreenshot', {format:'png', captureBeyondViewport:true, ...(clip ? {clip} : {})});
    writeFileSync(file, Buffer.from(result.data, 'base64'));
    return {file:basename(file), clip:clip || {width:this.width, height:this.height}};
  }
  runtimeErrors() { return this.browser.events.filter(e => e.sessionId === this.session).map(e => e.params.exceptionDetails); }
  async close() { await this.browser.call('Target.closeTarget', {targetId:this.target}); }
}
