// Import these pure DOM functions into the browser audit, or pass them to
// Playwright page.evaluate. No network or screenshot services are needed.
export function captureMathSource() {
  return [...document.querySelectorAll('article.paper-fulltext math')].map(m => ({id:m.id, html:m.outerHTML}));
}

export function collectMathEvidence() {
  const article=document.querySelector('article.paper-fulltext');
  if(!article)throw Error('Missing paper-fulltext article');
  const rect=e=>{const r=e.getBoundingClientRect();return {x:r.x,y:r.y,width:r.width,height:r.height,right:r.right,bottom:r.bottom};};
  const wrappers=[...article.querySelectorAll('[data-reading-math-scroll]')].map(e=>({math:e.querySelector('math')?.id,rect:rect(e),clientWidth:e.clientWidth,scrollWidth:e.scrollWidth,overflowX:getComputedStyle(e).overflowX,tabIndex:e.tabIndex}));
  const math=[...article.querySelectorAll('math')].map(m=>{
    const paragraph=m.closest('.ltx_p,.paper-paragraph,p,li')||article;
    const pStyle=getComputedStyle(paragraph),pRect=rect(paragraph);
    const available=paragraph.clientWidth-(parseFloat(pStyle.paddingLeft)||0)-(parseFloat(pStyle.paddingRight)||0);
    let clipping=null,scrolling=null;
    for(let p=m.parentElement;p&&p!==article.parentElement;p=p.parentElement){
      const style=getComputedStyle(p),r=rect(p),mr=rect(m);
      if(['auto','scroll'].includes(style.overflowX)&&p.scrollWidth>p.clientWidth+2){scrolling={class:p.className,clientWidth:p.clientWidth,scrollWidth:p.scrollWidth};break;}
      if(['hidden','clip'].includes(style.overflowX)&&(mr.right>r.right+2||mr.x<r.x-2)){clipping={class:p.className,overflow:style.overflowX};break;}
    }
    return {id:m.id,display:m.getAttribute('display')||'inline',rect:rect(m),paragraph:pRect,available,wrapped:!!m.closest('[data-reading-math-scroll]'),inTable:!!m.closest('table,.ltx_tabular,.ltx_equation'),clipping,scrolling};
  });
  return {viewport:innerWidth,documentWidth:document.documentElement.scrollWidth,mathCount:math.length,wrappers,math,justifiedParagraphs:[...article.querySelectorAll('.ltx_p,.paper-paragraph')].filter(p=>getComputedStyle(p).textAlign==='justify').length};
}

export function assertMathEvidence(report) {
  const errors=[];
  for(const m of report.math){
    if(m.clipping)errors.push({id:m.id,problem:'formula is clipped by non-scrollable ancestor',ancestor:m.clipping});
    if(m.display==='inline'&&!m.inTable&&m.rect.width>m.available+2&&!m.wrapped)errors.push({id:m.id,problem:'oversized inline formula has no scroll container',width:m.rect.width,available:m.available});
    if(m.display==='inline'&&!m.inTable&&m.wrapped&&m.rect.width<=m.available+2)errors.push({id:m.id,problem:'fitting inline formula was unnecessarily wrapped'});
    if(m.display==='inline'&&!m.inTable&&!m.wrapped&&(m.rect.right>m.paragraph.right+3||m.rect.x<m.paragraph.x-3))errors.push({id:m.id,problem:'unwrapped inline formula escapes paragraph'});
  }
  for(const w of report.wrappers){
    if(!['auto','scroll'].includes(w.overflowX))errors.push({id:w.math,problem:'wrapper does not allow horizontal scrolling'});
    if(w.scrollWidth<=w.clientWidth+2)errors.push({id:w.math,problem:'wrapper added without actual overflow'});
    if(w.tabIndex<0)errors.push({id:w.math,problem:'overflow not keyboard focusable'});
  }
  if(report.justifiedParagraphs)errors.push({problem:'Chinese paper paragraphs still use justification',count:report.justifiedParagraphs});
  return errors;
}

// Wide glyphs with fewer than 28 visible characters exercise the measurement
// criterion, and short-symbol geometry must remain exactly native.
export const regressionFixture=`<!doctype html><meta charset="utf-8"><style>
body{margin:20px}article{max-width:none}p{margin:16px 0;text-align:left;font:18px/1.8 sans-serif}
math{font-family:math}#wide{font-size:40px}
.tex-inline-math-scroll{display:block;max-width:100%;overflow-x:auto;margin:.35em 0}
</style><article class="paper-fulltext">
<p id="baseline">短公式 <math id="short"><msup><mi>x</mi><mn>2</mn></msup></math> 保持原生基线。</p>
<p>窄屏下需要滚动 <math id="wide"><semantics><mi>WWWWWWWWWWWWWWWWWW</mi><annotation encoding="application/x-tex">WWWWWWWWWWWWWWWWWW</annotation></semantics></math> 但宽屏应恢复行内。</p>
</article>`;

// Runner requirements:
// 1. Load fixture at 390px, await fonts.ready, snapshot every math.outerHTML and
//    #short.getBoundingClientRect() before injecting MATH_LAYOUT_SCRIPT.
// 2. Inject script, wait two animation frames; assert only #wide is wrapped,
//    no math.outerHTML changes, and all short formula x/y/width/height unchanged.
// 3. Resize to 1440px, wait two frames; assert no wrappers and same MathML.
// 4. Resize back to 390px; assert one wrapper and no nested wrappers.
// 5. On all 38 real reading pages at 1440 and 390px, run collectMathEvidence /
//    assertMathEvidence, compare mathematical snapshots before/after, save JSON.
// 6. Visually inspect LETTER #S7.p7 (user's screenshot), GRPO #S4.E3,
//    GSPO #S4.SS1, OPSD #S3.E6 and #S3.E9, both widths and light/dark as needed.
