// Run with: CHROME_BIN=google-chrome node tests/browser_audit.mjs --output-dir artifacts/paper-render
import {createHash} from 'node:crypto';
import {execFileSync} from 'node:child_process';
import {existsSync, mkdirSync, readFileSync, readdirSync, writeFileSync} from 'node:fs';
import {dirname, join, relative, resolve} from 'node:path';
import {fileURLToPath} from 'node:url';
import {Browser, serveSite} from './browser_support.mjs';
import {collectTableGeometry} from './browser_table_assertions.mjs';
import {captureMathSource, captureDiagramSource, collectMathEvidence, assertMathEvidence,
  regressionFixture, diagramRegressionFixture} from './browser_math_assertions.mjs';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const args = process.argv.slice(2);
if (args.includes('--help')) {
  console.log('Usage: node tests/browser_audit.mjs [--site-dir docs] [--output-dir artifacts/paper-render]\nCHROME_BIN selects Chrome. No npm packages, API keys, or user browser profile are used.');
  process.exit(0);
}
const options = {'--site-dir':join(root, 'docs'), '--output-dir':join(root, 'artifacts/paper-render')};
for (let i = 0; i < args.length; i += 2) {
  if (!(args[i] in options) || !args[i + 1]) throw new Error(`Invalid option: ${args[i]}`);
  options[args[i]] = resolve(args[i + 1]);
}
const output = options['--output-dir'], siteDir = options['--site-dir'];
mkdirSync(join(output, 'geometry'), {recursive:true});
mkdirSync(join(output, 'screenshots'), {recursive:true});
const report = {status:'running', startedAt:new Date().toISOString(), widths:[1440, 390], theme:'dark',
  siteDir, pages:[], library:[], fixtures:[], errors:[], screenshots:[]};
const json = (path, value) => writeFileSync(path, JSON.stringify(value, null, 2) + '\n');
const save = () => json(join(output, 'report.json'), report);
const digest = value => createHash('sha256').update(JSON.stringify(value ?? null)).digest('hex');
const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);
const inlineEvidenceEnabled = process.env.PAPER_RENDER_INLINE_EVIDENCE === '1';
const inlineImages = [];
function logResult(name, result) {
  console.log(`${result.status.toUpperCase()} ${name}: ${result.issues.length} issue(s)`);
  for (const issue of result.issues.slice(0, 5)) console.log(JSON.stringify({audit:name, ...issue}));
}
function snapshotDifferences(before, after) {
  const differences=[];
  for(let index=0;index<Math.max(before?.length||0,after?.length||0);index++) {
    const old=before?.[index],current=after?.[index];
    if(same(old,current))continue;
    const original=old?.html||'',updated=current?.html||'';
    let offset=0;
    while(offset<Math.min(original.length,updated.length)&&original[offset]===updated[offset])offset++;
    differences.push({index,id:current?.id||old?.id,offset,
      beforeExcerpt:original.slice(Math.max(0,offset-80),offset+160),
      afterExcerpt:updated.slice(Math.max(0,offset-80),offset+160),before:old,after:current});
  }
  return differences;
}
const cases = [
  {id:'2402.03300', target:'S2.T1', name:'deepseekmath-table-1'},
  {id:'2609.28589', target:'S6.T2', name:'onetrans-v2-table-2'},
  {id:'2405.07314', target:'S4.T3', name:'letter-table-3'},
  {id:'2405.07314', target:'S7.p7', name:'letter-inline-formulas'},
  {id:'2601.12681', target:'S3.F2.fig1', name:'hyformer-figure-2', widths:[390]},
  {id:'2609.20807', selector:'.ltx_listing.ltx_minipage', name:'score-centering-listing', widths:[390]},
  {id:'2510.11639', target:'p1.pic1', name:'onerec-think-diagram', widths:[390]},
  {id:'2609.31045', target:'S3.F2.pic1', name:'kuafu-diagram', widths:[390]},
  {id:'2402.17152', target:'alg1', name:'hstu-algorithm-1', widths:[390]},
  {id:'2306.13649', target:'alg1', name:'gkd-algorithm-1', widths:[390]},
  {id:'2609.29180', target:'S4.F9.pic1', name:'x-rec-diagram-9', widths:[390]},
  {id:'2609.28589', target:'S4.F3.pic1', name:'onetrans-v2-diagram-3', widths:[390]},
  {id:'2606.06260', target:'S6.T9', name:'onereason-table-9'},
  {id:'2607.26500', target:'S5.T2', name:'multidecoder-table-2'},
  {id:'2512.24762', target:'S6.T4', name:'openonerec-table-4'},
  {id:'2610.20913', target:'S2.T1', name:'transx-notation-table'},
  {id:'2610.20913', target:'S2.Ex17', name:'transx-amortization-formula'},
  {id:'2610.20913', target:'S4.F4', name:'transx-training-diagram'},
  {id:'2610.20913', target:'A5.T19', name:'transx-post-training-table'},
];
let browser, server;

async function capture(page, name, selector, inlinePriority = null) {
  const path = join(output, 'screenshots', name + '.png');
  const result = await page.screenshot(path, selector);
  const entry={...result,file:relative(output,path)};report.screenshots.push(entry);
  if(inlineEvidenceEnabled && inlinePriority !== null) {
    const jpeg=await page.screenshotJpeg(result,75);
    entry.jpeg={quality:jpeg.quality,beforeCapture:jpeg.beforeCapture,afterCapture:jpeg.afterCapture};
    inlineImages.push({name:name+'.jpg',priority:inlinePriority,data:jpeg.data,quality:jpeg.quality});
  }
}

function emitInlineEvidence() {
  const maxBytes=4_000_000;
  report.inlineEvidence={enabled:inlineEvidenceEnabled,maxBytes,emitted:[],skipped:[],imageLogBytes:0};
  if(!inlineEvidenceEnabled)return;
  // Reserve the final report and space for this small index. Image payloads
  // contain only screenshots of our public static reading pages.
  const budget=Math.max(0,maxBytes-Buffer.byteLength(JSON.stringify(report),'utf8')-20000);
  for(const image of inlineImages.sort((a,b)=>a.priority-b.priority||a.name.localeCompare(b.name))) {
    const line=`PAPER_RENDER_IMAGE ${JSON.stringify({file:image.name,mime:'image/jpeg',quality:image.quality,base64:image.data})}`;
    const bytes=Buffer.byteLength(line+'\n','utf8');
    if(report.inlineEvidence.imageLogBytes+bytes>budget) {report.inlineEvidence.skipped.push(image.name);continue;}
    console.log(line);
    report.inlineEvidence.imageLogBytes+=bytes;
    report.inlineEvidence.emitted.push(image.name);
  }
}

async function paperAudit(paper, width) {
  const name = `${paper.arxiv_id}-${width}-dark`;
  const result = {id:paper.arxiv_id, width, theme:'dark', status:'running', issues:[]};
  report.pages.push(result); save();
  let page;
  try {
    const source = readFileSync(join(siteDir, 'papers', paper.arxiv_id, 'index.html'), 'utf8');
    const sourceMathCount = (source.match(/<math\b/gi) || []).length;
    const beforeLoad = `document.addEventListener('DOMContentLoaded',()=>{window.__paperMathBefore=(${captureMathSource.toString()})();window.__paperDiagramsBefore=(${captureDiagramSource.toString()})();},{once:true});`;
    page = await browser.page(`${server.url}/papers/${paper.arxiv_id}/index.html`, {width, beforeLoad});
    const tables = await page.eval(`(${collectTableGeometry.toString()})()`);
    const math = await page.eval(`(${collectMathEvidence.toString()})()`);
    const before = await page.eval('window.__paperMathBefore');
    const after = await page.eval(`(${captureMathSource.toString()})()`);
    const diagramsBefore = await page.eval('window.__paperDiagramsBefore');
    const diagramsAfter = await page.eval(`(${captureDiagramSource.toString()})()`);
    result.issues.push(...tables.issues, ...assertMathEvidence(math));
    if (page.imageErrors.length) result.issues.push({kind:'images-failed-to-load', images:page.imageErrors});
    if (!Array.isArray(before) || !same(before, after)) result.issues.push({kind:'math-content-changed', beforeCount:before?.length, afterCount:after.length});
    if (after.length !== sourceMathCount) result.issues.push({kind:'math-node-count-mismatch', htmlSource:sourceMathCount, browser:after.length});
    if (!Array.isArray(diagramsBefore) || !same(diagramsBefore, diagramsAfter)) {
      const differences=snapshotDifferences(diagramsBefore,diagramsAfter);
      const artifact=`geometry/${name}-svg-diff.json`;
      json(join(output,artifact),{beforeCount:diagramsBefore?.length,afterCount:diagramsAfter.length,differences});
      result.issues.push({kind:'svg-content-changed',beforeCount:diagramsBefore?.length,afterCount:diagramsAfter.length,artifact,
        differences:differences.slice(0,5).map(({before,after,...summary})=>summary)});
    }
    const runtimeErrors = page.runtimeErrors();
    if (runtimeErrors.length) result.issues.push({kind:'browser-script-errors', exceptions:runtimeErrors});
    const document = await page.eval(`({width:innerWidth,scrollWidth:document.documentElement.scrollWidth,bodyWidth:document.body.scrollWidth})`);
    if (document.scrollWidth > width + 2 || document.bodyWidth > width + 2) result.issues.push({kind:'page-horizontal-overflow', ...document});
    const evidence = {paper:paper.arxiv_id, width, theme:'dark', document, tables, math,
      mathSource:{htmlCount:sourceMathCount, beforeCount:before?.length, afterCount:after.length, beforeHash:digest(before), afterHash:digest(after)},
      diagramSource:{beforeCount:diagramsBefore?.length, afterCount:diagramsAfter.length, beforeHash:digest(diagramsBefore), afterHash:digest(diagramsAfter)},
      runtimeErrors, imageErrors:page.imageErrors, imagePreparation:page.imagePreparation};
    json(join(output, 'geometry', name + '.json'), evidence);
    result.geometry = `geometry/${name}.json`; result.tables = tables.metrics; result.mathCount = math.mathCount;
    for (const example of cases.filter(c => c.id === paper.arxiv_id && (!c.widths || c.widths.includes(width)))) {
      const selector = example.selector || `[id=${JSON.stringify(example.target)}]`;
      const priority=example.widths?0:width===390?1:3;
      await capture(page, `${example.name}-${width}-dark`, selector, priority);
      const scrolls = await page.eval(`(()=>{const e=document.querySelector(${JSON.stringify(selector)}),candidates=new Set([e,...e.querySelectorAll('*')]);
        for(let p=e.parentElement;p&&p.matches('article.paper-fulltext')===false;p=p.parentElement)candidates.add(p);
        return [...candidates].filter(s=>s.scrollWidth>s.clientWidth+2&&['auto','scroll'].includes(getComputedStyle(s).overflowX)).map(s=>{s.scrollLeft=s.scrollWidth;return {id:s.id,client:s.clientWidth,scroll:s.scrollWidth,scrollLeft:s.scrollLeft};});})()`);
      if (scrolls.length) {
        await page.settle();
        await capture(page, `${example.name}-${width}-dark-scroll-end`, selector, width===390?2:4);
        await page.eval(`document.querySelectorAll('article.paper-fulltext *').forEach(e=>{if(e.scrollLeft)e.scrollLeft=0})`);
      }
      (result.focused || (result.focused=[])).push({name:example.name, selector, scrolls});
    }
    if (result.issues.length) {
      const first = result.issues.find(i => i.selector || i.id);
      const selector = first?.selector || (first?.id ? `[id=${JSON.stringify(first.id)}]` : 'article.paper-fulltext');
      await capture(page, `failure-${name}`, selector).catch(() => capture(page, `failure-${name}`, null));
    }
    result.status = result.issues.length ? 'failed' : 'passed';
  } catch (error) {
    result.status = 'failed'; result.issues.push({kind:'audit-exception', message:error.stack || String(error)});
    if (page) await capture(page, `failure-${name}`, null).catch(() => {});
  } finally { if (page) await page.close().catch(() => {}); save(); }
  logResult(name, result);
}

async function mathFixtureAudit() {
  const script = execFileSync('python3', ['-c', 'from paper_pipeline.reading_math import MATH_LAYOUT_SCRIPT; print(MATH_LAYOUT_SCRIPT)'],
    {cwd:root, encoding:'utf8', env:{...process.env, PYTHONDONTWRITEBYTECODE:'1'}});
  const result = {name:'measured-inline-math-width-and-native-baseline', status:'running', issues:[], states:[]};
  report.fixtures.push(result);
  let page;
  try {
    page = await browser.page(null, {width:390}); await page.setContent(regressionFixture);
    await page.eval(`document.body.style.backgroundColor='#141c25';document.body.style.color='#eef3f8'`);
    const original = await page.eval(`(${captureMathSource.toString()})()`);
    const rectangle = `(()=>{const r=document.querySelector('#short').getBoundingClientRect();return {x:r.x,y:r.y,width:r.width,height:r.height};})()`;
    const baseline = await page.eval(rectangle);
    await page.eval(script); await page.settle();
    for (const [width, expectedWrappers, label] of [[390, 1, 'initial-mobile'], [1440, 0, 'desktop'], [390, 1, 'mobile-again']]) {
      await page.resize(width); await page.settle();
      const evidence = await page.eval(`(${collectMathEvidence.toString()})()`);
      const current = await page.eval(`(${captureMathSource.toString()})()`);
      const short = await page.eval(rectangle);
      const nested = await page.eval(`document.querySelectorAll('[data-reading-math-scroll] [data-reading-math-scroll]').length`);
      result.issues.push(...assertMathEvidence(evidence));
      if (!same(original, current)) result.issues.push({kind:'fixture-math-content-changed', label});
      if (evidence.wrappers.length !== expectedWrappers || nested) result.issues.push({kind:'fixture-wrapper-count', label, expectedWrappers, actual:evidence.wrappers.length, nested});
      if (evidence.math.find(m => m.id === 'short')?.wrapped) result.issues.push({kind:'short-formula-wrapped', label});
      if (width === 390 && Object.keys(baseline).some(k => Math.abs(short[k] - baseline[k]) > 0.5)) result.issues.push({kind:'native-short-formula-baseline-changed', label, baseline, short});
      result.states.push({label, width, short, evidence});
      await capture(page, `math-fixture-${label}-dark`, 'article');
    }
    result.status = result.issues.length ? 'failed' : 'passed';
  } catch (error) { result.status = 'failed'; result.issues.push({kind:'fixture-exception', message:error.stack || String(error)}); }
  finally { if (page) await page.close().catch(() => {}); json(join(output, 'geometry/math-fixture.json'), result); save(); }
  logResult('math-fixture', result);
}

async function diagramFixtureAudit() {
  const script = execFileSync('python3', ['-c', 'from paper_pipeline.reading_math import MATH_LAYOUT_SCRIPT; print(MATH_LAYOUT_SCRIPT)'],
    {cwd:root, encoding:'utf8', env:{...process.env, PYTHONDONTWRITEBYTECODE:'1'}});
  const result = {name:'whole-diagram-scroll-and-native-icon', status:'running', issues:[], states:[]};
  report.fixtures.push(result);
  let page;
  try {
    page = await browser.page(null, {width:390}); await page.setContent(diagramRegressionFixture);
    await page.eval(`document.body.style.backgroundColor='#141c25';document.body.style.color='#eef3f8'`);
    const original = await page.eval(`(${captureDiagramSource.toString()})()`);
    const originalMath = await page.eval(`(${captureMathSource.toString()})()`);
    const rectangle = `(()=>{const r=document.querySelector('#short-diagram').getBoundingClientRect();return {x:r.x,y:r.y,width:r.width,height:r.height};})()`;
    const baseline = await page.eval(rectangle);
    await page.eval(script); await page.settle();
    for (const [width, expectedWrappers, label] of [[390,1,'initial-mobile'],[1440,0,'desktop'],[390,1,'mobile-again']]) {
      await page.resize(width); await page.settle();
      const evidence = await page.eval(`(${collectMathEvidence.toString()})()`);
      const current = await page.eval(`(${captureDiagramSource.toString()})()`);
      const currentMath = await page.eval(`(${captureMathSource.toString()})()`);
      const short = await page.eval(rectangle);
      const state = await page.eval(`(()=>{const wide=document.querySelector('#wide-diagram'),short=document.querySelector('#short-diagram');return {
        wrappers:document.querySelectorAll('[data-reading-diagram-scroll]').length,
        nested:document.querySelectorAll('[data-reading-diagram-scroll] [data-reading-diagram-scroll]').length,
        wideWrapped:!!wide.closest('[data-reading-diagram-scroll]'),shortWrapped:!!short.closest('[data-reading-diagram-scroll]'),
        diagramMathWrapped:!!document.querySelector('#diagram-math').closest('[data-reading-math-scroll]'),scrollWidth:document.documentElement.scrollWidth};})()`);
      result.issues.push(...assertMathEvidence(evidence));
      if (!same(original,current) || !same(originalMath,currentMath)) result.issues.push({kind:'fixture-diagram-content-changed',label});
      if (state.wrappers!==expectedWrappers || state.nested || state.wideWrapped!==!!expectedWrappers || state.shortWrapped || state.diagramMathWrapped) result.issues.push({kind:'fixture-diagram-wrapper-count',label,expectedWrappers,...state});
      if (Object.keys(baseline).some(k=>Math.abs(short[k]-baseline[k])>0.5)) result.issues.push({kind:'native-short-diagram-baseline-changed',label,baseline,short});
      if (state.scrollWidth>width+2) result.issues.push({kind:'fixture-diagram-page-overflow',label,...state});
      await capture(page, `diagram-fixture-${label}-dark`, 'article');
      let scrollEnd = null;
      if (expectedWrappers) {
        scrollEnd = await page.eval(`(()=>{const e=document.querySelector('[data-reading-diagram-scroll]');e.scrollLeft=e.scrollWidth;return {scrollLeft:e.scrollLeft,max:e.scrollWidth-e.clientWidth};})()`);
        if (scrollEnd.scrollLeft<1 || Math.abs(scrollEnd.scrollLeft-scrollEnd.max)>2) result.issues.push({kind:'fixture-diagram-scroll-end-unreachable',label,...scrollEnd});
        await page.settle(); await capture(page, `diagram-fixture-${label}-dark-scroll-end`, 'article');
        await page.eval(`document.querySelector('[data-reading-diagram-scroll]').scrollLeft=0`);
      }
      result.states.push({label,width,short,state,evidence,scrollEnd,svgHash:digest(current),mathHash:digest(currentMath)});
    }
    result.status=result.issues.length?'failed':'passed';
  } catch(error) {result.status='failed';result.issues.push({kind:'diagram-fixture-exception',message:error.stack||String(error)});}
  finally {if(page)await page.close().catch(()=>{});json(join(output,'geometry/diagram-fixture.json'),result);save();}
  logResult('diagram-fixture',result);
}

async function libraryAudit(catalog, width) {
  const result = {width, theme:'dark', status:'running', issues:[], states:[]}; report.library.push(result);
  let page;
  const keywords = paper => [paper.short_name, paper.title_zh, paper.title_en, paper.arxiv_id, ...(paper.companies || []), ...(paper.topics || [])].join(' ').toLowerCase();
  const expected = (order, query, topic) => [...catalog].sort((a,b) => (order === 'oldest' ? 1 : -1) * a.published_date.localeCompare(b.published_date))
    .filter(p => keywords(p).includes(query.toLowerCase().trim()) && (!topic || (p.topics || []).includes(topic))).map(p => p.arxiv_id);
  try {
    page = await browser.page(`${server.url}/papers/index.html`, {width});
    if (page.imageErrors.length) result.issues.push({kind:'images-failed-to-load', images:page.imageErrors});
    const initial = await page.eval(`(()=>{const controls=['paper-sort','paper-search','paper-topic'].map(id=>({id,present:!!document.getElementById(id)}));
      return {controls,order:document.querySelector('#paper-sort')?.value,entries:[...document.querySelectorAll('.paper-entry')].map(e=>({id:new URL(e.querySelector('h2 a').href).pathname.split('/').at(-2),date:e.dataset.publishedDate,time:e.querySelector('time[datetime]')?.getAttribute('datetime')}))};})()`);
    if (initial.controls.some(c => !c.present)) throw new Error('Library control missing: ' + JSON.stringify(initial.controls));
    if (initial.order !== 'newest') result.issues.push({kind:'default-sort-not-newest', value:initial.order});
    if (initial.entries.length !== catalog.length || !same(initial.entries.map(e=>e.id), expected('newest','',''))) result.issues.push({kind:'initial-library-order-or-count', initial});
    for (const entry of initial.entries) {
      const source = catalog.find(p => p.arxiv_id === entry.id);
      if (!source || !entry.date || entry.date !== source.published_date || entry.time !== entry.date) result.issues.push({kind:'library-date-metadata', entry});
    }
    const anchor = catalog.find(p => p.short_name.includes('OneRec') && p.topics?.length) || catalog.find(p => p.topics?.length);
    if (!anchor) throw new Error('No topic-bearing catalog entry to exercise combined filters');
    const query = anchor.short_name.includes('OneRec') ? 'OneRec' : anchor.short_name;
    const topic = anchor.topics[0];
    const states = [
      ['newest','',''], ['oldest','',''], ['newest',query,''], ['oldest',query,topic],
      ['newest',query,topic], ['newest','',topic], ['oldest','__no_such_paper_9fd628__',topic], ['newest','',''],
    ];
    for (let index = 0; index < states.length; index++) {
      const [order, search, selectedTopic] = states[index];
      await page.eval(`(()=>{for(const [id,value,event] of ${JSON.stringify([['paper-sort',order,'change'],['paper-search',search,'input'],['paper-topic',selectedTopic,'change']])}){const e=document.getElementById(id);e.value=value;e.dispatchEvent(new Event(event,{bubbles:true}));}})()`);
      await page.settle();
      const actual = await page.eval(`(()=>{const cards=[...document.querySelectorAll('.paper-entry')];return {
        visible:cards.filter(e=>!e.hidden&&e.getClientRects().length).map(e=>new URL(e.querySelector('h2 a').href).pathname.split('/').at(-2)),
        hiddenButRendered:cards.filter(e=>e.hidden&&e.getClientRects().length).length,count:document.querySelector('#paper-count')?.textContent,
        emptyHidden:document.querySelector('#paper-empty')?.hidden,width:innerWidth,scrollWidth:document.documentElement.scrollWidth};})()`);
      const wanted = expected(order, search, selectedTopic);
      const state = {order, search, topic:selectedTopic, expected:wanted, actual}; result.states.push(state);
      if (!same(wanted, actual.visible) || actual.hiddenButRendered || Number(actual.count?.match(/\d+/)?.[0]) !== wanted.length || actual.emptyHidden !== !!wanted.length || actual.scrollWidth > width + 2) result.issues.push({kind:'library-filter-or-sort-mismatch', state});
      if (index < 2) await capture(page, `library-${width}-dark-${order}`, '.paper-library');
    }
    if (page.runtimeErrors().length) result.issues.push({kind:'library-script-errors', exceptions:page.runtimeErrors()});
    result.status = result.issues.length ? 'failed' : 'passed';
  } catch (error) { result.status = 'failed'; result.issues.push({kind:'library-exception', message:error.stack || String(error)}); }
  finally { if (page) await page.close().catch(() => {}); json(join(output, `geometry/library-${width}.json`), result); save(); }
  logResult(`library-${width}-dark`, result);
}

save();
try {
  const catalog = JSON.parse(readFileSync(join(root, 'paper_catalog.json'), 'utf8')).papers;
  const published = JSON.parse(readFileSync(join(siteDir, 'papers/catalog.json'), 'utf8')).papers;
  const ids = rows => rows.map(p => p.arxiv_id).sort();
  if (!catalog.length || new Set(ids(catalog)).size !== catalog.length || !same(ids(catalog), ids(published))) throw new Error('Source and published catalog ID sets must match exactly');
  const actualPages = readdirSync(join(siteDir,'papers'), {withFileTypes:true})
    .filter(entry=>entry.isDirectory()&&existsSync(join(siteDir,'papers',entry.name,'index.html'))).map(entry=>entry.name).sort();
  if (!same(actualPages, ids(catalog))) throw new Error('Actual reading-page ID set differs from catalog');
  const missingDates = catalog.filter(p => !/^\d{4}-\d{2}-\d{2}$/.test(p.published_date || ''));
  if (missingDates.length) throw new Error('Missing catalog dates: ' + missingDates.map(p=>p.arxiv_id).join(', '));
  for (const paper of catalog) {
    if (published.find(p => p.arxiv_id === paper.arxiv_id).published_date !== paper.published_date) throw new Error('Published date mismatch: ' + paper.arxiv_id);
    readFileSync(join(siteDir, 'papers', paper.arxiv_id, 'index.html'));
  }
  report.catalog = {papers:catalog.length, ids:ids(catalog), missingDates:0, newest:[...catalog].sort((a,b)=>b.published_date.localeCompare(a.published_date))[0].published_date,
    oldest:[...catalog].sort((a,b)=>a.published_date.localeCompare(b.published_date))[0].published_date};
  server = await serveSite(siteDir); browser = new Browser({outputDir:output});
  report.browser = await browser.call('Browser.getVersion'); save();
  await mathFixtureAudit();
  await diagramFixtureAudit();
  for (const width of report.widths) {
    await libraryAudit(catalog, width);
    for (const paper of catalog) await paperAudit(paper, width);
  }
  const requiredScreenshots = cases.flatMap(c => (c.widths || report.widths).map(w => `${c.name}-${w}-dark.png`));
  const missingScreenshots = requiredScreenshots.filter(name => !report.screenshots.some(s => s.file === `screenshots/${name}`));
  if (missingScreenshots.length) report.errors.push({kind:'required-screenshots-missing', files:missingScreenshots});
  if (report.pages.length !== catalog.length * report.widths.length) report.errors.push({kind:'incomplete-page-coverage'});
} catch (error) { report.errors.push({kind:'fatal', message:error.stack || String(error)}); }
finally {
  if (browser) await browser.close().catch(error => report.errors.push({kind:'browser-close', message:String(error)}));
  if (server) await server.close().catch(error => report.errors.push({kind:'server-close', message:String(error)}));
  report.finishedAt = new Date().toISOString();
  report.status = report.errors.length || [...report.pages, ...report.library, ...report.fixtures].some(r => r.status !== 'passed') ? 'failed' : 'passed';
  emitInlineEvidence();
  save();
}
console.log('PAPER_RENDER_REPORT '+JSON.stringify(report));
console.log(JSON.stringify({status:report.status, papers:report.catalog?.papers, pageRuns:report.pages.length,
  failedPageRuns:report.pages.filter(p=>p.status !== 'passed').length, screenshots:report.screenshots.length, report:join(output,'report.json')}));
for (const error of report.errors) console.error(JSON.stringify(error));
process.exitCode = report.status === 'passed' ? 0 : 1;
