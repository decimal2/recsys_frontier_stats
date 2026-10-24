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
      // Prepare lazy images before any audit snapshots or application DCL
      // listeners run. HTML img elements can live inside SVG foreignObjects;
      // changing their loading attribute after a snapshot changes svg.outerHTML.
      const prepareImages = `document.addEventListener('DOMContentLoaded',()=>{
        window.__auditImagePreparation=[...document.images].map(image=>{
          const previous=image.getAttribute('loading');image.loading='eager';
          return {id:image.id,src:image.currentSrc||image.src,previous,loading:image.getAttribute('loading'),insideSvg:!!image.closest('svg')};
        });
      },{once:true});`;
      await page.call('Page.addScriptToEvaluateOnNewDocument', {source:prepareImages+'\n'+beforeLoad});
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
    this.imagePreparation = await this.eval('window.__auditImagePreparation || []');
    this.imageErrors = await this.eval(`(async()=>{
      const images=[...document.images],errors=[];
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
  async frameGeometry(selector) {
    return this.eval(`(()=>{
      const rect=e=>{const r=e.getBoundingClientRect();return {x:r.x,y:r.y,width:r.width,height:r.height,right:r.right,bottom:r.bottom};};
      const e=${selector ? `document.querySelector(${JSON.stringify(selector)})` : 'null'};
      if(${!!selector}&&!e)throw Error('Screenshot target missing');
      const targetRect=e?rect(e):null;
      let visible=targetRect?{left:Math.max(0,targetRect.x),top:Math.max(0,targetRect.y),right:Math.min(innerWidth,targetRect.right),bottom:Math.min(innerHeight,targetRect.bottom)}:null;
      if(e)for(let p=e.parentElement;p;p=p.parentElement){
        const style=getComputedStyle(p),r=rect(p);
        if(['auto','scroll','hidden','clip'].includes(style.overflowX)){visible.left=Math.max(visible.left,r.x+p.clientLeft);visible.right=Math.min(visible.right,r.x+p.clientLeft+p.clientWidth);}
        if(['auto','scroll','hidden','clip'].includes(style.overflowY)){visible.top=Math.max(visible.top,r.y+p.clientTop);visible.bottom=Math.min(visible.bottom,r.y+p.clientTop+p.clientHeight);}
      }
      if(visible){visible.width=Math.max(0,visible.right-visible.left);visible.height=Math.max(0,visible.bottom-visible.top);}
      return {width:innerWidth,height:innerHeight,clientWidth:document.documentElement.clientWidth,
        documentHeight:document.documentElement.scrollHeight,scrollX,scrollY,targetRect,visible,
        target:e?{id:e.id,tag:e.tagName,text:e.textContent.trim().slice(0,120)}:null};
    })()`);
  }
  assertFrameStable(before, after) {
    const keys=['width','height','clientWidth','documentHeight','scrollX','scrollY'];
    const moved=keys.some(key=>Math.abs(before[key]-after[key])>1)||
      (before.targetRect&&['x','y','width','height'].some(key=>Math.abs(before.targetRect[key]-after.targetRect?.[key])>1));
    if(moved)throw Error('Screenshot changed layout: '+JSON.stringify({before,after}));
  }
  async screenshot(file, selector = null, maxHeight = 2000) {
    mkdirSync(resolve(file, '..'), {recursive:true});
    if (selector) {
      const initial=await this.frameGeometry(selector);
      const height=Math.ceil(Math.min(maxHeight,Math.max(600,initial.targetRect.height+160)));
      // Resize first, then measure/scroll again. Never reuse document-space
      // crop coordinates across captureBeyondViewport's virtual viewport.
      await this.resize(this.width,height);await this.settle();
      await this.eval(`(()=>{const r=document.querySelector(${JSON.stringify(selector)}).getBoundingClientRect();window.scrollTo({left:0,top:Math.max(0,r.top+scrollY-80),behavior:'instant'});})()`);
      await this.settle();
    }
    const before=await this.frameGeometry(selector);
    if(selector) {
      const requiredHeight=Math.min(before.targetRect.height,before.height-160);
      if(before.visible.width<1||before.visible.height<requiredHeight-2)throw Error('Screenshot target is not visible: '+JSON.stringify(before));
    }
    const result = await this.call('Page.captureScreenshot', {format:'png', captureBeyondViewport:false});
    writeFileSync(file, Buffer.from(result.data, 'base64'));
    const after=await this.frameGeometry(selector);this.assertFrameStable(before,after);
    return {file:basename(file),selector,mode:'visible-viewport',beforeCapture:before,afterCapture:after};
  }
  async screenshotJpeg(frame, quality=75) {
    const before=await this.frameGeometry(frame.selector);
    this.assertFrameStable(frame.afterCapture,before);
    const result=await this.call('Page.captureScreenshot',{format:'jpeg',quality,captureBeyondViewport:false});
    const after=await this.frameGeometry(frame.selector);this.assertFrameStable(before,after);
    return {...result,quality,beforeCapture:before,afterCapture:after};
  }
  runtimeErrors() { return this.browser.events.filter(e => e.sessionId === this.session).map(e => e.params.exceptionDetails); }
  async close() { await this.browser.call('Target.closeTarget', {targetId:this.target}); }
}
